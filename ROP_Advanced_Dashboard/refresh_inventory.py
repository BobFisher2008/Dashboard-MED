from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import apply_missing_as_zero, build_status_snapshot, load_project_data, map_inventory, parse_inventory, save_json
from warehouse import branch_rop_wide, save_snapshot_files, status_tables, write_tables


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh inventory and ROP status for the Python/Power BI dashboard.")
    parser.add_argument("--inventory", required=True, help="XLSX, XLSB or CSV inventory export")
    parser.add_argument("--snapshot-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--excess-threshold", type=float, default=2.0)
    parser.add_argument("--missing-as-zero", action="store_true")
    parser.add_argument("--powerbi", action="store_true", help="Also export readable CSVs to powerbi_data/")
    args = parser.parse_args()

    project = Path(args.project)
    data_dir = project / "data"
    frames = load_project_data(data_dir)
    dim_sku = frames["dim_sku"]
    dim_branch = frames["dim_branch"]
    alias_mapping = frames["alias_mapping"]
    rop_wide = branch_rop_wide(frames["fact_branch_rop"], dim_branch, dim_sku)

    inventory, parse_meta = parse_inventory(args.inventory)
    inventory["SnapshotDate"] = pd.to_datetime(inventory["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(args.snapshot_date))
    inventory["SourceFile"] = Path(args.inventory).name
    mapped, review, mapping_stats = map_inventory(inventory, alias_mapping, dim_sku, dim_branch)
    status = build_status_snapshot(dim_sku, mapped, rop_wide, args.snapshot_date, args.excess_threshold)
    if args.missing_as_zero:
        status = apply_missing_as_zero(status)

    mapped["SnapshotDate"] = pd.to_datetime(mapped["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(args.snapshot_date)).dt.date.astype(str)
    mapped["DateKey"] = pd.to_datetime(mapped["SnapshotDate"]).dt.strftime("%Y%m%d").astype("int32")
    mapped["Scope"] = mapping_stats["scope"]
    mapped["SourceFile"] = Path(args.inventory).name

    tables = status_tables(status, dim_branch, mapped, data_dir=data_dir)
    write_tables(
        {**tables, "fact_inventory_snapshot": mapped, "mapping_review": review},
        data_dir=data_dir, powerbi=args.powerbi,
    )

    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata.update({
        "snapshot_date": args.snapshot_date,
        "inventory_parser": parse_meta,
        "mapping": mapping_stats,
        "status_counts": {str(k): int(v) for k, v in status["Status"].value_counts().to_dict().items()},
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "on_hand_total_matched": float(status["OnHand"].sum(skipna=True)),
        "missing_inventory_sku": int((~status["HasInventory"]).sum()),
        "excess_threshold": args.excess_threshold,
        "missing_as_zero": args.missing_as_zero,
        "source_files": {**metadata.get("source_files", {}), "inventory": Path(args.inventory).name},
        "history_dates": [pd.Timestamp(str(k)).date().isoformat() for k in sorted(tables["fact_status_history"]["DateKey"].unique())],
    })
    save_json(metadata_path, metadata)

    save_snapshot_files(mapped, status, review, args.snapshot_date, snapshot_dir=project / "snapshots")

    print(json.dumps({
        "snapshot_date": args.snapshot_date,
        "scope": mapping_stats["scope"],
        "matched_rows": mapping_stats["matched_rows"],
        "unmatched_rows": mapping_stats["unmatched_rows"],
        "mapping_rate": mapping_stats["mapping_rate_rows"],
        "rop_gap": float(status["ROPGap"].sum(skipna=True)),
        "critical_ve_sku": int(status["IsCritical"].sum()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
