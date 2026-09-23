"""The ROP ETL as a Dagster asset graph.

    rop_workbook -----> branch_rop ---------+-----------------------+
                                            v                       v
    inventory_export -----------------> inventory_snapshot ---> batch_expiry ----+
                                            |                                    |
                                            +-------------> status_history ------+
                                                                                 v
    sku_category_csv, ved_classification_csv --------------------------------> warehouse_db -> (checks)

Each asset calls the same function the CLI calls, so ``python update_rop.py``
and a Dagster run do exactly the same thing. The assets are "files in place":
they rewrite ``data/*.parquet`` rather than handing DataFrames to an IO
manager, because the dashboard, the tests and Power BI already read those
files. What Dagster adds is lineage, run history, retries and metadata.
"""
# NOTE: no ``from __future__ import annotations`` here. Dagster validates the
# ``context`` parameter by comparing the annotation object itself, so a
# postponed (string) annotation is rejected with
# "Cannot annotate `context` parameter with type AssetExecutionContext".

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from dagster import (
    AssetExecutionContext,
    AssetKey,
    Config,
    DataVersion,
    MetadataValue,
    Output,
    asset,
    observable_source_asset,
)
from pydantic import Field

import expiry
import refresh_inventory
import update_rop
import warehouse
from watch_inventory import file_sha256, is_inventory_file, snapshot_date_from_name

from .resources import DuckDBWarehouse, RopProject

GROUP_SOURCE = "source"
GROUP_WAREHOUSE = "warehouse"


# --------------------------------------------------------------------------
# Sources - observed, never written by Dagster
# --------------------------------------------------------------------------
def _file_version(path: Path) -> DataVersion:
    """Content hash, so an asset only re-runs when the file really changed."""
    if not path.exists():
        return DataVersion("missing")
    return DataVersion(file_sha256(path))


def _inbox_files(project: RopProject) -> list[Path]:
    """Inventory files sitting directly in inbox/, oldest first."""
    if not project.inbox.exists():
        return []
    files = [p for p in project.inbox.iterdir() if p.is_file() and is_inventory_file(p)]
    return sorted(files, key=lambda p: p.stat().st_mtime)


@observable_source_asset(
    name="rop_workbook",
    group_name=GROUP_SOURCE,
    description="ROP final.xlsb - the branch ROP sheet, one row per SKU x branch.",
)
def rop_workbook(project: RopProject) -> DataVersion:
    return _file_version(project.rop_path)


@observable_source_asset(
    name="sku_category_csv",
    group_name=GROUP_SOURCE,
    description="SKU category.csv - the business-owned category list that overrides dim_sku.Category.",
)
def sku_category_csv(project: RopProject) -> DataVersion:
    return _file_version(project.category_path)


@observable_source_asset(
    name="ved_classification_csv",
    group_name=GROUP_SOURCE,
    description="VED ангилал.csv - the business-owned V / E / D list that overrides the VED of every SKU it lists.",
)
def ved_classification_csv(project: RopProject) -> DataVersion:
    return _file_version(project.ved_path)


@observable_source_asset(
    name="inventory_export",
    group_name=GROUP_SOURCE,
    description="The newest inventory export waiting in inbox/ (xlsx / xlsb / csv).",
)
def inventory_export(project: RopProject) -> DataVersion:
    candidates = _inbox_files(project)
    if not candidates:
        return DataVersion("empty")
    return DataVersion(file_sha256(candidates[-1]))


# --------------------------------------------------------------------------
# Branch ROP
# --------------------------------------------------------------------------
@asset(
    name="branch_rop",
    group_name=GROUP_WAREHOUSE,
    deps=[AssetKey("rop_workbook")],
    description="dim_sku / dim_branch / fact_branch_rop from the final ROP workbook.",
    compute_kind="pandas",
)
def branch_rop(context: AssetExecutionContext, project: RopProject) -> Output[dict[str, Any]]:
    if not project.rop_path.exists():
        raise FileNotFoundError(f"ROP workbook not found: {project.rop_path}")
    context.log.info("Loading branch ROP from %s", project.rop_path.name)
    summary = update_rop.load_rop(project.rop_path, powerbi=project.powerbi_export)
    return Output(
        summary,
        metadata={
            "source_file": summary["source_file"],
            "sku_count": summary["sku_count"],
            "branch_count": summary["branch_count"],
            "fact_branch_rop_rows": summary["branch_rop_rows"],
            "network_rop_total": summary["network_rop_total"],
            "corrected_branch_rop_total": summary["corrected_branch_rop_total"],
            "missing_rop_sku": summary["missing_rop_sku"],
            "ved_from_file_rows": summary["ved_from_file_rows"],
        },
    )


# --------------------------------------------------------------------------
# Inventory snapshot
# --------------------------------------------------------------------------
class InventoryRefreshConfig(Config):
    """Which export to load. The inbox sensor fills this in per run."""

    inventory_file: str | None = Field(
        default=None,
        description="Path to the export. Empty = take the oldest file in inbox/.",
    )
    snapshot_date: str | None = Field(
        default=None,
        description="YYYY-MM-DD. Empty = read it from the file name, else the file's modified date.",
    )
    excess_threshold: float | None = Field(
        default=None, description="Overrides the project resource's excess threshold."
    )
    missing_as_zero: bool | None = Field(
        default=None, description="Treat SKUs with no inventory line as zero stock."
    )
    archive: bool = Field(
        default=True,
        description="Move the file to inbox/processed (or inbox/failed) when the run ends.",
    )


def _resolve_snapshot_date(path: Path, configured: str | None, log) -> str:
    if configured:
        return configured
    from_name = snapshot_date_from_name(path.name)
    if from_name is not None:
        log.info("Snapshot date %s read from the file name", from_name.isoformat())
        return from_name.isoformat()
    fallback = datetime.fromtimestamp(path.stat().st_mtime).date()
    log.warning("No date in %s; using the file modified date %s", path.name, fallback.isoformat())
    return fallback.isoformat()


def _archive(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{stamp}_{path.name}"
    shutil.move(str(path), dest)
    return dest


@asset(
    name="inventory_snapshot",
    group_name=GROUP_WAREHOUSE,
    deps=[AssetKey("branch_rop"), AssetKey("inventory_export")],
    description="fact_sku_status / fact_inventory_snapshot for one export, appended to fact_status_history.",
    compute_kind="pandas",
)
def inventory_snapshot(
    context: AssetExecutionContext,
    config: InventoryRefreshConfig,
    project: RopProject,
) -> Output[dict[str, Any]]:
    project.ensure_inbox()
    if config.inventory_file:
        path = Path(config.inventory_file)
        if not path.is_absolute():
            path = project.root / path
    else:
        waiting = _inbox_files(project)
        if not waiting:
            raise FileNotFoundError(
                f"No inventory export configured and nothing in {project.inbox}. "
                "Drop a file in inbox/ or set inventory_file in the run config."
            )
        path = waiting[0]
    if not path.exists():
        raise FileNotFoundError(f"Inventory export not found: {path}")

    snapshot_date = _resolve_snapshot_date(path, config.snapshot_date, context.log)
    threshold = config.excess_threshold if config.excess_threshold is not None else project.excess_threshold
    missing_as_zero = config.missing_as_zero if config.missing_as_zero is not None else project.missing_as_zero

    context.log.info("Refreshing from %s (snapshot %s)", path.name, snapshot_date)
    digest = file_sha256(path)
    try:
        summary = refresh_inventory.refresh(
            inventory=path,
            snapshot_date=snapshot_date,
            project=project.root,
            excess_threshold=threshold,
            missing_as_zero=missing_as_zero,
            powerbi=project.powerbi_export,
        )
    except Exception:
        if config.archive and path.exists() and path.parent == project.inbox:
            dest = _archive(path, project.failed_dir)
            context.log.error("Refresh failed; moved %s to %s", path.name, dest.name)
        raise

    archived = None
    if config.archive and path.exists() and path.parent == project.inbox:
        archived = _archive(path, project.processed_dir).name
        context.log.info("Moved %s to processed/%s", path.name, archived)

    summary = {**summary, "sha256": digest, "archived_as": archived}
    return Output(
        summary,
        metadata={
            "snapshot_date": summary["snapshot_date"],
            "source_file": summary["source_file"],
            "sha256": digest,
            "scope": summary["scope"],
            "inventory_rows": summary["inventory_rows"],
            "fact_sku_status_rows": summary["status_rows"],
            "fact_status_history_rows": summary["history_rows"],
            "mapping_rate": summary["mapping_rate"],
            "unmatched_rows": summary["unmatched_rows"],
            "rop_gap": summary["rop_gap"],
            "on_hand_total": summary["on_hand_total"],
            "critical_ve_sku": summary["critical_ve_sku"],
            "status_counts": MetadataValue.json(summary["status_counts"]),
        },
    )


# --------------------------------------------------------------------------
# Batch expiry
# --------------------------------------------------------------------------
@asset(
    name="batch_expiry",
    group_name=GROUP_WAREHOUSE,
    deps=[AssetKey("inventory_snapshot"), AssetKey("branch_rop")],
    description=(
        "fact_batch_expiry: one row per SKU x branch x expiry date - expiry status, "
        "FEFO rank and sell-through projection - appended to fact_batch_expiry_history."
    ),
    compute_kind="pandas",
)
def batch_expiry(context: AssetExecutionContext, project: RopProject) -> Output[dict[str, Any]]:
    # warehouse_db rebuilds warehouse.duckdb right after, so skip it here.
    summary = expiry.refresh(project.data_dir, config=project.expiry_config_path, refresh_db=False)
    if not summary["has_expiry_dates"]:
        context.log.warning("%s has no expiry dates; every batch is UNKNOWN", summary["source_file"])
    context.log.info(
        "%s batches, %s at risk, %.1f%% of lines dated",
        summary["batch_rows"], summary["at_risk_batches"], 100 * summary["expiry_completeness"],
    )
    return Output(
        summary,
        metadata={
            "snapshot_date": summary["snapshot_date"],
            "source_file": summary["source_file"],
            "batch_rows": summary["batch_rows"],
            "expiry_completeness": round(summary["expiry_completeness"], 4),
            "expired_qty": summary["status_qty"]["EXPIRED"],
            "pull_batches": summary["pull_batches"],
            "at_risk_batches": summary["at_risk_batches"],
            "projected_waste_qty": summary["projected_waste_qty"],
            "usable_without_demand_data_batches": summary["usable_without_demand_data_batches"],
            "negative_qty_batches": summary["negative_qty_batches"],
            "implausible_expiry_lines": summary["implausible_expiry_lines"],
            "config_version": summary["config_version"],
            "status_batches": MetadataValue.json(summary["status_batches"]),
            "status_qty": MetadataValue.json(summary["status_qty"]),
        },
    )


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
@asset(
    name="status_history",
    group_name=GROUP_WAREHOUSE,
    deps=[AssetKey("inventory_snapshot")],
    description="Rebuild fact_status_history from every snapshots/status_*.csv.gz.",
    compute_kind="duckdb",
)
def status_history(context: AssetExecutionContext, project: RopProject) -> Output[dict[str, Any]]:
    checks = warehouse.rebuild_history(project.data_dir)
    dates = checks.get("history_dates", [])
    rows = int(checks.get("fact_status_history_rows", 0))
    context.log.info("History covers %s snapshot dates", len(dates))
    return Output(
        {"history_dates": dates, "rows": rows},
        metadata={
            "fact_status_history_rows": rows,
            "snapshot_dates": len(dates),
            "latest_snapshot": str(dates[-1]) if dates else "none",
        },
    )


# --------------------------------------------------------------------------
# The unified database
# --------------------------------------------------------------------------
@asset(
    name="warehouse_db",
    group_name=GROUP_WAREHOUSE,
    deps=[
        AssetKey("status_history"), AssetKey("batch_expiry"),
        AssetKey("sku_category_csv"), AssetKey("ved_classification_csv"),
    ],
    description="The unified database: every star-schema table rebuilt into data/warehouse.duckdb.",
    compute_kind="duckdb",
)
def warehouse_db(
    context: AssetExecutionContext,
    project: RopProject,
    unified_db: DuckDBWarehouse,
) -> Output[dict[str, Any]]:
    categories = project.category_path if project.category_path.exists() else None
    if categories is None:
        context.log.warning("%s not found; keeping the existing dim_sku categories", project.category_csv)
    ved = project.ved_path if project.ved_path.exists() else None
    if ved is None:
        context.log.warning("%s not found; keeping the existing VED classes", project.ved_csv)
    checks = warehouse.build(categories=categories, powerbi=project.powerbi_export, data_dir=project.data_dir, ved=ved)

    rows = unified_db.table_rows() if unified_db.path.exists() else {}
    size_mb = round(unified_db.path.stat().st_size / 1e6, 1) if unified_db.path.exists() else 0.0
    metadata_path = project.data_dir / "metadata.json"
    meta = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}

    context.log.info("warehouse.duckdb: %s tables, %s MB", len(rows), size_mb)
    return Output(
        {"checks": checks, "tables": rows},
        metadata={
            "database": str(unified_db.path),
            "size_mb": size_mb,
            "table_count": len(rows),
            "total_rows": int(sum(rows.values())),
            "snapshot_date": str(meta.get("snapshot_date", "unknown")),
            "snapshot_dates": len(meta.get("history_dates", [])),
            "integrity_ok": bool(checks.get("all_ok", False)),
            "failed_checks": MetadataValue.json(checks.get("failed", [])),
            "rows_per_table": MetadataValue.json(rows),
        },
    )
