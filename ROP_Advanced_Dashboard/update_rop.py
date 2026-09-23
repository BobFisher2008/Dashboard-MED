from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from common import build_status_snapshot, excluded_mask, load_abc_xyz_file, load_project_data, match_rop_skus, save_json
from warehouse import (
    DATA_DIR,
    apply_sku_ved,
    branch_rop_wide,
    ensure_branches,
    save_status_snapshot,
    status_tables,
    to_fact_branch_rop,
    write_tables,
)

BASE = Path(__file__).resolve().parent
ROP_FILE = BASE / "ROP final.xlsb"
ABC_XYZ_FILE = BASE / "ABC XYZ.xlsx"
DETAIL_ROP_SOURCE = "Зассан дэлгэрэнгүй томьёо"
FALLBACK_ROP_SOURCE = "Нэгтгэл дэх fallback ROP"


def load_abc_xyz(path: str | Path = ABC_XYZ_FILE, data_dir: Path = DATA_DIR, refresh_db: bool = True) -> dict[str, Any]:
    """Write fact_abc_xyz_file from «ABC XYZ.xlsx»: the branch ABC / XYZ class
    that fills the ROP workbook's missing classes in the dashboard.

    Returns the stats; also stored under "abc_xyz_file" in metadata.json."""
    frames = load_project_data(data_dir)
    table, stats = load_abc_xyz_file(path, frames["dim_sku"], frames["dim_branch"], frames.get("alias_mapping"))
    write_tables({"fact_abc_xyz_file": table}, data_dir=data_dir, refresh_db=refresh_db)
    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata["abc_xyz_file"] = stats
    save_json(metadata_path, metadata)
    return stats


def load_rop(rop: str | Path = ROP_FILE, powerbi: bool = False) -> dict[str, Any]:
    """Load the final branch ROP workbook into the warehouse.

    Returns the summary that the CLI prints; also the payload the Dagster
    ``branch_rop`` asset turns into run metadata."""
    rop = Path(rop)
    frames = load_project_data(DATA_DIR)
    dim_sku = frames["dim_sku"].copy()
    dim_branch = frames["dim_branch"].copy()
    fact_inventory = frames["fact_inventory_snapshot"].copy()
    metadata = json.loads((DATA_DIR / "metadata.json").read_text(encoding="utf-8"))

    # 1. Read the final ROP detail (one row per SKU x branch). Rows are joined
    #    to dim_sku by product name: the workbook's own «SKU ID» numbering
    #    differs from the SKU master's.
    detail = pd.read_excel(rop, sheet_name="Нэгтгэл", engine="pyxlsb")
    detail, sku_match = match_rop_skus(detail, dim_sku, frames.get("alias_mapping"))
    detail = detail.loc[~excluded_mask(detail["Салбар"])].copy()

    # 2. Keep every existing Branch_ID (history refers to them) and add new ones.
    existing_branch_id = dict(zip(dim_branch["BranchName"].astype(str), dim_branch["Branch_ID"].astype(str)))
    next_id = max(int(x[1:]) for x in existing_branch_id.values() if x[:1] == "B" and x[1:].isdigit()) + 1
    for b in sorted(detail["Салбар"].dropna().astype(str).unique()):
        if b not in existing_branch_id:
            existing_branch_id[b] = f"B{next_id:03d}"
            next_id += 1
    new_branches = pd.DataFrame({"BranchName": list(existing_branch_id), "Branch_ID": list(existing_branch_id.values())})
    dim_branch = ensure_branches(dim_branch, new_branches)

    # 3. Build fact_branch_rop from the final ROP values.
    def num(col: str) -> pd.Series:
        return pd.to_numeric(detail[col], errors="coerce").fillna(0)

    fact = pd.DataFrame({
        "SKU_ID": detail["SKU_ID"].astype(int),
        "Branch_ID": detail["Салбар"].astype(str).map(existing_branch_id),
        "BranchName": detail["Салбар"].astype(str),
        "ROP": num("Автомат захиалгын түвшин (ROP)"),
        "AvgMonthlyDemand_Branch": num("Сарын дундаж тоо"),
        "DemandSD": num("Сарын STDEV.S"),
        "LeadTimeMean": num("LT ашигласан (сар)"),
        "LeadTimeSD": num("Lead Time STDEV.S"),
        "Sales7M": num("7 сарын дун.борлуулалтын дүн"),
        "ABC": detail["ABC"].fillna(""),
        "XYZ": detail["XYZ"].fillna(""),
        "ABC_XYZ": detail["ABC-XYZ"].fillna(""),
        "Z_VED": num("Z VED"),
        "Z_ABCXYZ": num("Z ABC-XYZ"),
        "Z_Final": num("Z эцсийн"),
        "VED": detail["VED"].fillna("Тодорхойгүй"),
    })
    fact = fact[fact["Branch_ID"].notna()].reset_index(drop=True)
    fact["HasROP"] = fact["ROP"] > 0
    fact_branch_rop = to_fact_branch_rop(fact)  # also drops duplicate SKU x branch rows
    # SKUs listed in the VED file keep that VED; the workbook's stays in VED_ROP.
    fact_branch_rop = apply_sku_ved(fact_branch_rop, dim_sku)
    ved_overridden = int((fact_branch_rop["VED"] != fact_branch_rop["VED_ROP"]).sum())

    # 4. Update the network-level ROP in dim_sku (sum of branch ROP per SKU).
    network_rop = fact_branch_rop.groupby("SKU_ID")["ROP"].sum()
    dim_sku["ROP_Corrected"] = dim_sku["SKU_ID"].map(network_rop).fillna(0)
    in_new = dim_sku["SKU_ID"].isin(network_rop.index)
    dim_sku.loc[in_new, "ROP_Used"] = dim_sku.loc[in_new, "ROP_Corrected"]
    dim_sku.loc[in_new, "ROP_Source"] = DETAIL_ROP_SOURCE
    dim_sku.loc[in_new, "ROP_Confidence"] = "Өндөр"
    # A SKU that an earlier load gave a detail ROP but this workbook no longer
    # lists goes back to the master's own ROP instead of keeping a stale one.
    stale = ~in_new & dim_sku["ROP_Source"].eq(DETAIL_ROP_SOURCE)
    dim_sku.loc[stale, "ROP_Used"] = pd.to_numeric(dim_sku.loc[stale, "ROP_Current"], errors="coerce").fillna(0)
    dim_sku.loc[stale, "ROP_Source"] = FALLBACK_ROP_SOURCE
    dim_sku.loc[stale, "ROP_Confidence"] = "Бага"
    dim_sku["HasROP"] = dim_sku["ROP_Used"] > 0
    dim_sku["HasDetailROP"] = dim_sku["ROP_Source"].eq(DETAIL_ROP_SOURCE)

    # 5. Remap inventory Branch_ID to the branch dimension (no-op for existing branches).
    fact_inventory["Branch_ID"] = fact_inventory["BranchName"].astype(str).map(existing_branch_id).fillna(fact_inventory["Branch_ID"])

    # 6. Recompute the current status with the updated ROP.
    snapshot_date = metadata.get("snapshot_date", "2026-09-07")
    excess_threshold = float(metadata.get("excess_threshold", 2.0))
    rop_wide = branch_rop_wide(fact_branch_rop, dim_branch, dim_sku)
    status = build_status_snapshot(dim_sku, fact_inventory, rop_wide, snapshot_date, excess_threshold)

    # 7. Write outputs.
    tables = status_tables(status, dim_branch, fact_inventory)
    write_tables({
        **tables,
        "dim_sku": dim_sku,
        "fact_branch_rop": fact_branch_rop,
        "fact_inventory_snapshot": fact_inventory,
    }, powerbi=powerbi)
    # History is rebuilt from the snapshot files: without this, the next
    # inventory refresh would bring back this date's status under the old ROP.
    save_status_snapshot(status, snapshot_date)

    # 8. Update metadata.
    metadata.update({
        "source_files": {**metadata.get("source_files", {}), "rop": rop.name},
        "rop_sku_match": sku_match,
        "sku_count": int(len(dim_sku)),
        "branch_count": int((~tables["dim_branch"]["IsNetwork"]).sum()),
        "branch_rop_rows": int(len(fact_branch_rop)),
        "network_rop_total": float(dim_sku["ROP_Used"].sum()),
        "corrected_branch_rop_total": float(fact_branch_rop["ROP"].sum()),
        "status_counts": {str(k): int(v) for k, v in status["Status"].value_counts().to_dict().items()},
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "on_hand_total_matched": float(status["OnHand"].sum(skipna=True)),
        "missing_inventory_sku": int((~status["HasInventory"]).sum()),
        "missing_rop_sku": int((~dim_sku["HasROP"]).sum()),
        "low_confidence_rop_sku": int((dim_sku["ROP_Confidence"] == "Бага").sum()),
    })
    save_json(DATA_DIR / "metadata.json", metadata)
    abc_xyz = load_abc_xyz() if ABC_XYZ_FILE.exists() else None

    return {
        "source_file": rop.name,
        "sku_count": int(len(dim_sku)),
        "branch_count": int((~tables["dim_branch"]["IsNetwork"]).sum()),
        "branch_rop_rows": int(len(fact_branch_rop)),
        "network_rop_total": float(dim_sku["ROP_Used"].sum()),
        "corrected_branch_rop_total": float(fact_branch_rop["ROP"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "missing_rop_sku": int((~dim_sku["HasROP"]).sum()),
        "ved_from_file_rows": ved_overridden,
        "rop_rows_matched_by_name": sku_match["matched_rows"],
        "rop_rows_unmatched": sku_match["unmatched_rows"],
        "rop_rows_with_other_workbook_id": sku_match["rows_with_other_workbook_id"],
        "abc_xyz_file_rows": abc_xyz["rows"] if abc_xyz else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the final branch ROP workbook into the warehouse.")
    parser.add_argument("--rop", type=Path, default=ROP_FILE)
    parser.add_argument("--powerbi", action="store_true", help="Also export readable CSVs to powerbi_data/")
    parser.add_argument("--abc-xyz-only", action="store_true", help="Only reload «ABC XYZ.xlsx» (fact_abc_xyz_file)")
    args = parser.parse_args()
    result = load_abc_xyz() if args.abc_xyz_only else load_rop(args.rop, args.powerbi)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
