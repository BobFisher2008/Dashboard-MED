"""Asset checks over the unified database.

``warehouse.integrity_checks()`` already produces every number we care about,
so the checks run it once against the freshly built warehouse and split the
result into the groups a reader would act on differently:

* ``referential_integrity`` - orphan keys and duplicate grain. ERROR: the star
  schema is wrong and the dashboard will silently drop or double-count rows.
* ``measure_ranges`` - negative ROP gaps, coverage outside 0..1, rows on
  excluded branches, a VED that is not a class or differs from the VED file.
  ERROR: a calculation or a filter is broken.
* ``row_counts`` - no table is empty. ERROR: a load produced nothing.
* ``mapping_rate`` - share of inventory rows matched to a SKU. WARN only,
  because a new export legitimately brings unmapped locations.
* ``snapshot_freshness`` - age of the newest snapshot. WARN only.
* ``expiry_completeness`` - share of inventory lines with a valid expiry
  date. WARN only: the fix is in the ERP data, not the pipeline.
* ``expiry_data_quality`` - negative batch stock and implausible expiry
  dates (more than 10 years ahead or before 2000). WARN only.

The batch expiry fact's own integrity - same snapshot as fact_sku_status,
batches adding up to OnHand, a FEFO rank on exactly the usable batches - is
part of ``referential_integrity`` / ``measure_ranges`` via integrity_checks().
"""
# NOTE: no ``from __future__ import annotations`` here. Dagster validates the
# ``context`` parameter by comparing the annotation object itself, so a
# postponed (string) annotation is rejected with
# "Cannot annotate `context` parameter with type AssetExecutionContext".

import json
from datetime import date, datetime
from typing import Any

import pandas as pd
from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetCheckSpec,
    AssetCheckExecutionContext,
    AssetKey,
    MetadataValue,
    asset_check,
    multi_asset_check,
)

import warehouse

from .resources import RopProject

WAREHOUSE = AssetKey("warehouse_db")

# How stale the newest snapshot may be before the freshness check warns.
MAX_SNAPSHOT_AGE_DAYS = 10
# Share of inventory lines that must carry a valid expiry date.
MIN_EXPIRY_COMPLETENESS = 0.90


def _run_integrity(project: RopProject) -> dict[str, Any]:
    con = warehouse.connect(project.data_dir)
    try:
        return warehouse.integrity_checks(con)
    finally:
        con.close()


def _subset(checks: dict[str, Any], predicate) -> dict[str, Any]:
    return {k: v for k, v in checks.items() if k not in ("failed", "all_ok") and predicate(k)}


def _is_referential(key: str) -> bool:
    return "orphan" in key or key.endswith("_unique_id") or key.endswith("_unique_key")


def _is_measure(key: str) -> bool:
    return "negative" in key or "invalid" in key or "excluded_branch" in key


def _is_row_count(key: str) -> bool:
    """A table's size, not a violation counter.

    ``negative_rop_rows`` and ``*_excluded_branch_rows`` also end in ``_rows``
    but are meant to be zero, so they belong to the measure group.
    """
    return key.endswith("_rows") and not _is_measure(key)


@multi_asset_check(
    specs=[
        AssetCheckSpec(
            name="referential_integrity",
            asset=WAREHOUSE,
            description="Every fact key resolves to a dimension row, and each fact's grain is unique.",
        ),
        AssetCheckSpec(
            name="measure_ranges",
            asset=WAREHOUSE,
            description="No negative ROP gaps, coverage within 0..1, no rows on excluded branches, VED as in the VED file.",
        ),
        AssetCheckSpec(
            name="row_counts",
            asset=WAREHOUSE,
            description="No dimension or fact table came out empty.",
        ),
    ],
)
def warehouse_integrity(context: AssetCheckExecutionContext, project: RopProject):
    checks = _run_integrity(project)
    context.log.info("integrity_checks: %s", "OK" if checks["all_ok"] else f"failed {checks['failed']}")

    referential = _subset(checks, _is_referential)
    bad_referential = [k for k in referential if k in checks["failed"]]
    yield AssetCheckResult(
        check_name="referential_integrity",
        passed=not bad_referential,
        severity=AssetCheckSeverity.ERROR,
        metadata={"failed": MetadataValue.json(bad_referential), **{k: v for k, v in referential.items()}},
    )

    measures = _subset(checks, _is_measure)
    bad_measures = [k for k in measures if k in checks["failed"]]
    yield AssetCheckResult(
        check_name="measure_ranges",
        passed=not bad_measures,
        severity=AssetCheckSeverity.ERROR,
        metadata={"failed": MetadataValue.json(bad_measures), **{k: v for k, v in measures.items()}},
    )

    counts = _subset(checks, _is_row_count)
    empty = sorted(k for k, v in counts.items() if not v)
    yield AssetCheckResult(
        check_name="row_counts",
        passed=not empty,
        severity=AssetCheckSeverity.ERROR,
        metadata={"empty_tables": MetadataValue.json(empty), **{k: int(v) for k, v in counts.items()}},
    )


@asset_check(
    asset=WAREHOUSE,
    name="mapping_rate",
    blocking=False,
    description="Share of inventory rows matched to a SKU in the latest refresh.",
)
def mapping_rate_check(project: RopProject) -> AssetCheckResult:
    path = project.data_dir / "metadata.json"
    if not path.exists():
        return AssetCheckResult(passed=False, severity=AssetCheckSeverity.WARN,
                                metadata={"error": "data/metadata.json is missing"})
    meta = json.loads(path.read_text(encoding="utf-8"))
    mapping = meta.get("mapping", {})
    rate = mapping.get("mapping_rate_rows")
    if rate is None:
        return AssetCheckResult(passed=False, severity=AssetCheckSeverity.WARN,
                                metadata={"error": "metadata.json has no mapping.mapping_rate_rows"})
    # Reported as a fraction in some runs and as a percentage in others.
    fraction = rate / 100 if rate > 1 else rate
    return AssetCheckResult(
        passed=fraction >= 0.90,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "mapping_rate": round(fraction, 4),
            "matched_rows": mapping.get("matched_rows", 0),
            "unmatched_rows": mapping.get("unmatched_rows", 0),
            "scope": str(mapping.get("scope", "unknown")),
            "threshold": 0.90,
        },
    )


@asset_check(
    asset=WAREHOUSE,
    name="snapshot_freshness",
    blocking=False,
    description=f"The newest snapshot is no more than {MAX_SNAPSHOT_AGE_DAYS} days old.",
)
def snapshot_freshness_check(project: RopProject) -> AssetCheckResult:
    path = project.data_dir / "metadata.json"
    if not path.exists():
        return AssetCheckResult(passed=False, severity=AssetCheckSeverity.WARN,
                                metadata={"error": "data/metadata.json is missing"})
    meta = json.loads(path.read_text(encoding="utf-8"))
    dates = meta.get("history_dates") or ([meta["snapshot_date"]] if meta.get("snapshot_date") else [])
    if not dates:
        return AssetCheckResult(passed=False, severity=AssetCheckSeverity.WARN,
                                metadata={"error": "no snapshot dates recorded"})
    latest = max(pd.Timestamp(d).date() for d in dates)
    age = (date.today() - latest).days
    return AssetCheckResult(
        passed=age <= MAX_SNAPSHOT_AGE_DAYS,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "latest_snapshot": latest.isoformat(),
            "age_days": age,
            "max_age_days": MAX_SNAPSHOT_AGE_DAYS,
            "snapshot_count": len(dates),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def _expiry_summary(project: RopProject) -> tuple[dict[str, Any] | None, str | None]:
    path = project.data_dir / "metadata.json"
    if not path.exists():
        return None, "data/metadata.json is missing"
    summary = json.loads(path.read_text(encoding="utf-8")).get("expiry")
    if not summary:
        return None, "metadata.json has no expiry section; batch_expiry has not run"
    return summary, None


@asset_check(
    asset=WAREHOUSE,
    name="expiry_completeness",
    blocking=False,
    description=f"At least {MIN_EXPIRY_COMPLETENESS:.0%} of inventory lines carry a valid expiry date.",
)
def expiry_completeness_check(project: RopProject) -> AssetCheckResult:
    summary, error = _expiry_summary(project)
    if summary is None:
        return AssetCheckResult(passed=False, severity=AssetCheckSeverity.WARN, metadata={"error": error})
    completeness = float(summary["expiry_completeness"])
    return AssetCheckResult(
        passed=completeness >= MIN_EXPIRY_COMPLETENESS,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "expiry_completeness": round(completeness, 4),
            "threshold": MIN_EXPIRY_COMPLETENESS,
            "unknown_batches": int(summary["status_batches"].get("UNKNOWN", 0)),
            "snapshot_date": str(summary.get("snapshot_date", "unknown")),
        },
    )


@asset_check(
    asset=WAREHOUSE,
    name="expiry_data_quality",
    blocking=False,
    description="No batch with negative stock and no implausible expiry date.",
)
def expiry_data_quality_check(project: RopProject) -> AssetCheckResult:
    summary, error = _expiry_summary(project)
    if summary is None:
        return AssetCheckResult(passed=False, severity=AssetCheckSeverity.WARN, metadata={"error": error})
    negative = int(summary["negative_qty_batches"])
    implausible = int(summary["implausible_expiry_lines"])
    return AssetCheckResult(
        passed=negative == 0 and implausible == 0,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "negative_qty_batches": negative,
            "implausible_expiry_lines": implausible,
            "snapshot_date": str(summary.get("snapshot_date", "unknown")),
        },
    )


warehouse_checks = [
    warehouse_integrity, mapping_rate_check, snapshot_freshness_check,
    expiry_completeness_check, expiry_data_quality_check,
]
