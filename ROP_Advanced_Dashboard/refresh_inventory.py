from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import build_status_snapshot, load_project_data, map_inventory, parse_inventory, save_json


def write_outputs(frame: pd.DataFrame, project: Path, name: str) -> None:
    data_dir = project / "data"
    powerbi_dir = project / "powerbi_data"
    data_dir.mkdir(exist_ok=True)
    powerbi_dir.mkdir(exist_ok=True)
    frame.to_csv(data_dir / f"{name}.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
    frame.to_csv(powerbi_dir / f"{name}.csv", index=False, encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh inventory and ROP status for the Python/Power BI dashboard.")
    parser.add_argument("--inventory", required=True, help="XLSX, XLSB or CSV inventory export")
    parser.add_argument("--snapshot-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--excess-threshold", type=float, default=2.0)
    parser.add_argument("--missing-as-zero", action="store_true")
    args = parser.parse_args()

    project = Path(args.project)
    frames = load_project_data(project / "data")
    dim_sku = frames["dim_sku"]
    dim_branch = frames["dim_branch"]
    fact_branch_rop = frames["fact_branch_rop"]
    alias_mapping = frames["alias_mapping"]

    inventory, parse_meta = parse_inventory(args.inventory)
    inventory["SnapshotDate"] = pd.to_datetime(inventory["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(args.snapshot_date))
    inventory["SourceFile"] = Path(args.inventory).name
    mapped, review, mapping_stats = map_inventory(inventory, alias_mapping, dim_sku, dim_branch)
    status = build_status_snapshot(dim_sku, mapped, fact_branch_rop, args.snapshot_date, args.excess_threshold)

    if args.missing_as_zero:
        missing = ~status["HasInventory"]
        status.loc[missing, ["OnHand", "OnOrder", "Backorder", "InventoryPosition"]] = 0.0
        status.loc[missing, "ROPGap"] = status.loc[missing, "ROP"].clip(lower=0)
        status.loc[missing, "CoverageRatio"] = 0.0
        status.loc[missing & (status["ROP"] > 0), "Status"] = "Тасарсан"
        status.loc[missing & (status["ROP"] <= 0), "Status"] = "ROP байхгүй"
        status.loc[missing, "DataMatch"] = "Missing treated as zero"
        status.loc[missing, "HasInventory"] = True

    mapped["SnapshotDate"] = pd.to_datetime(mapped["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(args.snapshot_date)).dt.date.astype(str)
    mapped["Scope"] = mapping_stats["scope"]
    mapped["SourceFile"] = Path(args.inventory).name

    write_outputs(mapped, project, "fact_inventory_snapshot")
    write_outputs(status, project, "fact_sku_status")
    write_outputs(review, project, "mapping_review")

    metadata_path = project / "data" / "metadata.json"
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
    })
    save_json(project / "data" / "metadata.json", metadata)
    save_json(project / "powerbi_data" / "metadata.json", metadata)

    snapshot_dir = project / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    stamp = args.snapshot_date.replace("-", "")
    mapped.to_csv(snapshot_dir / f"inventory_{stamp}.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
    status.to_csv(snapshot_dir / f"status_{stamp}.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
    review.to_csv(snapshot_dir / f"mapping_review_{stamp}.csv", index=False, encoding="utf-8-sig")

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
