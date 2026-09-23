from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

import expiry
from common import apply_missing_as_zero, build_status_snapshot, load_project_data, map_inventory, parse_inventory, save_json
from warehouse import branch_rop_wide, save_snapshot_files, status_tables, write_tables

BASE_DIR = Path(__file__).resolve().parent


def refresh(
    inventory: str | Path,
    snapshot_date: str,
    project: str | Path = BASE_DIR,
    excess_threshold: float = 2.0,
    missing_as_zero: bool = False,
    powerbi: bool = False,
) -> dict[str, Any]:
    """Load one inventory export into the warehouse and refresh the status facts.

    Returns the summary that the CLI prints; also the payload the Dagster
    ``inventory_snapshot`` asset turns into run metadata."""
    project = Path(project)
    data_dir = project / "data"
    frames = load_project_data(data_dir)
    dim_sku = frames["dim_sku"]
    dim_branch = frames["dim_branch"]
    alias_mapping = frames["alias_mapping"]
    rop_wide = branch_rop_wide(frames["fact_branch_rop"], dim_branch, dim_sku)

    parsed, parse_meta = parse_inventory(inventory)
    parsed["SnapshotDate"] = pd.to_datetime(parsed["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(snapshot_date))
    parsed["SourceFile"] = Path(inventory).name
    mapped, review, mapping_stats = map_inventory(parsed, alias_mapping, dim_sku, dim_branch)
    status = build_status_snapshot(dim_sku, mapped, rop_wide, snapshot_date, excess_threshold)
    if missing_as_zero:
        status = apply_missing_as_zero(status)

    mapped["SnapshotDate"] = pd.to_datetime(mapped["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(snapshot_date)).dt.date.astype(str)
    mapped["DateKey"] = pd.to_datetime(mapped["SnapshotDate"]).dt.strftime("%Y%m%d").astype("int32")
    mapped["Scope"] = mapping_stats["scope"]
    mapped["SourceFile"] = Path(inventory).name

    tables = status_tables(status, dim_branch, mapped, data_dir=data_dir)
    write_tables(
        {**tables, "fact_inventory_snapshot": mapped, "mapping_review": review},
        data_dir=data_dir, powerbi=powerbi,
    )

    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata.update({
        "snapshot_date": snapshot_date,
        "inventory_parser": parse_meta,
        "mapping": mapping_stats,
        "status_counts": {str(k): int(v) for k, v in status["Status"].value_counts().to_dict().items()},
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "on_hand_total_matched": float(status["OnHand"].sum(skipna=True)),
        "missing_inventory_sku": int((~status["HasInventory"]).sum()),
        "excess_threshold": excess_threshold,
        "missing_as_zero": missing_as_zero,
        "source_files": {**metadata.get("source_files", {}), "inventory": Path(inventory).name},
        "history_dates": [pd.Timestamp(str(k)).date().isoformat() for k in sorted(tables["fact_status_history"]["DateKey"].unique())],
    })
    save_json(metadata_path, metadata)

    save_snapshot_files(mapped, status, review, snapshot_date, snapshot_dir=project / "snapshots")

    return {
        "snapshot_date": snapshot_date,
        "source_file": Path(inventory).name,
        "scope": mapping_stats["scope"],
        "inventory_rows": int(len(mapped)),
        "status_rows": int(len(tables["fact_sku_status"])),
        "history_rows": int(len(tables["fact_status_history"])),
        "matched_rows": mapping_stats["matched_rows"],
        "unmatched_rows": mapping_stats["unmatched_rows"],
        "mapping_rate": mapping_stats["mapping_rate_rows"],
        "rop_gap": float(status["ROPGap"].sum(skipna=True)),
        "on_hand_total": float(status["OnHand"].sum(skipna=True)),
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "status_counts": {str(k): int(v) for k, v in status["Status"].value_counts().to_dict().items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh inventory and ROP status for the Python/Power BI dashboard.")
    parser.add_argument("--inventory", required=True, help="XLSX, XLSB or CSV inventory export")
    parser.add_argument("--snapshot-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--project", default=str(BASE_DIR))
    parser.add_argument("--excess-threshold", type=float, default=2.0)
    parser.add_argument("--missing-as-zero", action="store_true")
    parser.add_argument("--powerbi", action="store_true", help="Also export readable CSVs to powerbi_data/")
    args = parser.parse_args()

    summary = refresh(
        inventory=args.inventory,
        snapshot_date=args.snapshot_date,
        project=args.project,
        excess_threshold=args.excess_threshold,
        missing_as_zero=args.missing_as_zero,
        powerbi=args.powerbi,
    )
    # Dagster runs this as its own asset (batch_expiry); the CLI - and so the
    # inventory watcher - runs it here, so fact_batch_expiry never lags fact_sku_status.
    batches = expiry.refresh(Path(args.project) / "data")
    report = {k: summary[k] for k in
              ["snapshot_date", "scope", "matched_rows", "unmatched_rows", "mapping_rate", "rop_gap", "critical_ve_sku"]}
    report["expiry"] = {k: batches[k] for k in
                        ["batch_rows", "expiry_completeness", "at_risk_batches", "projected_waste_qty"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
