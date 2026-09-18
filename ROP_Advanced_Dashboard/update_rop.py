from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import build_status_snapshot, excluded_mask, load_project_data, save_json
from warehouse import DATA_DIR, branch_rop_wide, ensure_branches, status_tables, to_fact_branch_rop, write_tables

BASE = Path(__file__).resolve().parent
ROP_FILE = BASE / "ROP final.xlsb"


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the final branch ROP workbook into the warehouse.")
    parser.add_argument("--rop", type=Path, default=ROP_FILE)
    parser.add_argument("--powerbi", action="store_true", help="Also export readable CSVs to powerbi_data/")
    args = parser.parse_args()

    frames = load_project_data(DATA_DIR)
    dim_sku = frames["dim_sku"].copy()
    dim_branch = frames["dim_branch"].copy()
    fact_inventory = frames["fact_inventory_snapshot"].copy()
    metadata = json.loads((DATA_DIR / "metadata.json").read_text(encoding="utf-8"))

    # 1. Read the final ROP detail (one row per SKU x branch).
    detail = pd.read_excel(args.rop, sheet_name="Нэгтгэл", engine="pyxlsb")
    detail["SKU_ID"] = pd.to_numeric(detail["SKU ID"], errors="coerce").astype("Int64")
    detail = detail[detail["SKU_ID"].notna()].copy()
    detail["SKU_ID"] = detail["SKU_ID"].astype(int)
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

    # 4. Update the network-level ROP in dim_sku (sum of branch ROP per SKU).
    network_rop = fact_branch_rop.groupby("SKU_ID")["ROP"].sum()
    dim_sku["ROP_Corrected"] = dim_sku["SKU_ID"].map(network_rop).fillna(0)
    in_new = dim_sku["SKU_ID"].isin(network_rop.index)
    dim_sku.loc[in_new, "ROP_Used"] = dim_sku.loc[in_new, "ROP_Corrected"]
    dim_sku.loc[in_new, "ROP_Source"] = "Зассан дэлгэрэнгүй томьёо"
    dim_sku.loc[in_new, "ROP_Confidence"] = "Өндөр"
    dim_sku["HasROP"] = dim_sku["ROP_Used"] > 0
    dim_sku["HasDetailROP"] = dim_sku["ROP_Source"].eq("Зассан дэлгэрэнгүй томьёо")

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
    }, powerbi=args.powerbi)

    # 8. Update metadata.
    metadata.update({
        "source_files": {**metadata.get("source_files", {}), "rop": Path(args.rop).name},
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

    print(json.dumps({
        "sku_count": int(len(dim_sku)),
        "branch_rop_rows": int(len(fact_branch_rop)),
        "network_rop_total": float(dim_sku["ROP_Used"].sum()),
        "corrected_branch_rop_total": float(fact_branch_rop["ROP"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "critical_ve_sku": int(status["IsCritical"].sum()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
