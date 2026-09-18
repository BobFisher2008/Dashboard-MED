from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from common import (
    branch_group,
    build_status_snapshot,
    clean_category,
    is_excluded_branch,
    load_project_data,
    save_json,
)

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
PBI_DIR = BASE / "powerbi_data"
ROP_FILE = BASE / "ROP final.xlsb"


def write_csv(frame: pd.DataFrame, name: str) -> None:
    frame.to_csv(DATA_DIR / f"{name}.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
    frame.to_csv(PBI_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")


def main() -> None:
    frames = load_project_data(DATA_DIR)
    dim_sku = frames["dim_sku"].copy()
    dim_sku["Category"] = dim_sku["Category"].map(clean_category)
    dim_branch = frames["dim_branch"].copy()
    fact_inventory = frames["fact_inventory_snapshot"].copy()
    metadata = json.loads((DATA_DIR / "metadata.json").read_text(encoding="utf-8"))

    # 1. Read the final ROP detail (one row per SKU x branch).
    detail = pd.read_excel(ROP_FILE, sheet_name="Нэгтгэл", engine="pyxlsb")
    detail["SKU_ID"] = pd.to_numeric(detail["SKU ID"], errors="coerce").astype("Int64")
    detail = detail[detail["SKU_ID"].notna()].copy()
    detail["SKU_ID"] = detail["SKU_ID"].astype(int)
    detail = detail.loc[~detail["Салбар"].map(is_excluded_branch)].copy()

    # 2. Rebuild dim_branch, preserving existing Branch_IDs and adding new ones.
    existing_branch_id = dict(zip(dim_branch["BranchName"].astype(str), dim_branch["Branch_ID"].astype(str)))
    branches = sorted(detail["Салбар"].dropna().astype(str).unique())
    next_id = max(int(x[1:]) for x in existing_branch_id.values() if x.startswith("B")) + 1
    for b in branches:
        if b not in existing_branch_id:
            existing_branch_id[b] = f"B{next_id:03d}"
            next_id += 1

    new_dim_branch = pd.DataFrame({"BranchName": ["NETWORK"] + branches})
    new_dim_branch["Branch_ID"] = new_dim_branch["BranchName"].map(
        lambda x: "NETWORK" if x == "NETWORK" else existing_branch_id[x]
    )
    new_dim_branch["BranchGroup"] = new_dim_branch["BranchName"].map(branch_group)
    new_dim_branch["IsNetwork"] = new_dim_branch["Branch_ID"].eq("NETWORK")

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
        "Category": detail["Категори"].map(clean_category),
    })
    fact = fact[fact["Branch_ID"].notna()].reset_index(drop=True)
    fact["HasROP"] = fact["ROP"] > 0

    # 4. Update the network-level ROP in dim_sku (sum of branch ROP per SKU).
    network_rop = fact.groupby("SKU_ID")["ROP"].sum()
    dim_sku["ROP_Corrected"] = dim_sku["SKU_ID"].map(network_rop).fillna(0)
    in_new = dim_sku["SKU_ID"].isin(network_rop.index)
    dim_sku.loc[in_new, "ROP_Used"] = dim_sku.loc[in_new, "ROP_Corrected"]
    dim_sku.loc[in_new, "ROP_Source"] = "Зассан дэлгэрэнгүй томьёо"
    dim_sku.loc[in_new, "ROP_Confidence"] = "Өндөр"
    dim_sku["HasROP"] = dim_sku["ROP_Used"] > 0
    dim_sku["HasDetailROP"] = dim_sku["ROP_Source"].eq("Зассан дэлгэрэнгүй томьёо")

    # 5. Remap inventory Branch_ID to the new dim_branch (no-op for existing branches).
    if "Branch_ID" in fact_inventory:
        fact_inventory["Branch_ID"] = (
            fact_inventory["BranchName"].astype(str).map(existing_branch_id).fillna(fact_inventory["Branch_ID"])
        )

    # 6. Recompute status with the updated ROP.
    snapshot_date = metadata.get("snapshot_date", "2026-09-07")
    excess_threshold = float(metadata.get("excess_threshold", 2.0))
    status = build_status_snapshot(dim_sku, fact_inventory, fact, snapshot_date, excess_threshold)

    # 7. Write outputs.
    write_csv(dim_sku, "dim_sku")
    write_csv(new_dim_branch, "dim_branch")
    write_csv(fact, "fact_branch_rop")
    write_csv(status, "fact_sku_status")

    # 8. Update metadata.
    metadata.update({
        "source_files": {**metadata.get("source_files", {}), "rop": ROP_FILE.name},
        "sku_count": int(len(dim_sku)),
        "branch_count": int((~new_dim_branch["IsNetwork"]).sum()),
        "branch_rop_rows": int(len(fact)),
        "network_rop_total": float(dim_sku["ROP_Used"].sum()),
        "corrected_branch_rop_total": float(fact["ROP"].sum()),
        "status_counts": {str(k): int(v) for k, v in status["Status"].value_counts().to_dict().items()},
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "on_hand_total_matched": float(status["OnHand"].sum(skipna=True)),
        "missing_inventory_sku": int((~status["HasInventory"]).sum()),
        "missing_rop_sku": int((~dim_sku["HasROP"]).sum()),
        "low_confidence_rop_sku": int((dim_sku["ROP_Confidence"] == "Бага").sum()),
    })
    save_json(DATA_DIR / "metadata.json", metadata)
    save_json(PBI_DIR / "metadata.json", metadata)

    print(json.dumps({
        "sku_count": int(len(dim_sku)),
        "branch_count": int((~new_dim_branch["IsNetwork"]).sum()),
        "branch_rop_rows": int(len(fact)),
        "network_rop_total": float(dim_sku["ROP_Used"].sum()),
        "corrected_branch_rop_total": float(fact["ROP"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "critical_ve_sku": int(status["IsCritical"].sum()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
