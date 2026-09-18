from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from common import (
    VED_Z,
    apply_category_map,
    branch_group,
    build_status_snapshot,
    clean_category,
    is_excluded_branch,
    load_category_map,
    map_inventory,
    normalize_product_name,
    parse_inventory,
    save_json,
)
from warehouse import DATA_DICTIONARY, build_static_dims, status_tables, to_fact_branch_rop, write_tables


def load_rop_master(v1_workbook: str | Path) -> pd.DataFrame:
    raw = pd.read_excel(v1_workbook, sheet_name="ROP_мастер", header=3, engine="openpyxl")
    rename = {
        "SKU ID": "SKU_ID",
        "Нэр төрөл": "SKU_Name",
        "Master key": "MasterKey",
        "Категори": "Category",
        "VED": "VED",
        "Lead Time (сар)": "LeadTime_Months",
        "LT төлөв": "LeadTime_Status",
        "7 сарын борлуулалтын дүн": "Sales_7M",
        "Сарын дундаж эрэлт - нийт": "AvgMonthlyDemand",
        "ROP current total": "ROP_Current",
        "ROP corrected total": "ROP_Corrected",
        "ROP used": "ROP_Used",
        "ROP source": "ROP_Source",
        "ROP confidence": "ROP_Confidence",
        "Active branch count": "ActiveBranchCount",
    }
    df = raw.rename(columns=rename)[list(rename.values())].copy()
    df["SKU_ID"] = pd.to_numeric(df["SKU_ID"], errors="coerce").astype("Int64")
    df = df[df["SKU_ID"].notna()].copy()
    df["SKU_ID"] = df["SKU_ID"].astype(int)
    for col in ["LeadTime_Months", "Sales_7M", "AvgMonthlyDemand", "ROP_Current", "ROP_Corrected", "ROP_Used", "ActiveBranchCount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["MasterKey"] = df["MasterKey"].fillna(df["SKU_Name"].map(normalize_product_name))
    df["VED"] = df["VED"].fillna("Тодорхойгүй")
    df["Category"] = df["Category"].map(clean_category)
    df["ROP_Used"] = df["ROP_Used"].fillna(0)
    df["HasROP"] = df["ROP_Used"] > 0
    df["HasDetailROP"] = df["ROP_Source"].eq("Зассан дэлгэрэнгүй томьёо")
    df["ROP_Confidence"] = df["ROP_Confidence"].fillna("Бага")
    return df.sort_values("SKU_ID").reset_index(drop=True)


def load_alias_mapping(v1_workbook: str | Path) -> pd.DataFrame:
    raw = pd.read_excel(v1_workbook, sheet_name="Alias_маппинг", header=3, engine="openpyxl")
    rename = {
        "Normalized key": "NormalizedKey",
        "SKU ID": "SKU_ID",
        "Master name": "MasterName",
        "Mapping type": "MappingType",
        "Notes": "Notes",
    }
    df = raw.rename(columns=rename)[list(rename.values())].copy()
    df["SKU_ID"] = pd.to_numeric(df["SKU_ID"], errors="coerce").astype("Int64")
    df = df[df["SKU_ID"].notna() & df["NormalizedKey"].notna()].copy()
    df["SKU_ID"] = df["SKU_ID"].astype(int)
    df["NormalizedKey"] = df["NormalizedKey"].astype(str)
    return df.drop_duplicates(["NormalizedKey", "SKU_ID"]).reset_index(drop=True)


def load_branch_rop(rop_workbook: str | Path, dim_sku: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    detail = pd.read_excel(rop_workbook, sheet_name="ROP дэлгэрэнгүй", engine="pyxlsb")
    detail["SKU ID"] = pd.to_numeric(detail["SKU ID"], errors="coerce").astype("Int64")
    detail = detail[detail["SKU ID"].notna()].copy()
    detail["SKU ID"] = detail["SKU ID"].astype(int)
    detail = detail.loc[~detail["Салбар"].map(is_excluded_branch)].copy()

    sku_ved = dim_sku.set_index("SKU_ID")["VED"].to_dict()
    detail["VED_Full"] = detail["SKU ID"].map(sku_ved).fillna(detail["VED"]).fillna("Тодорхойгүй")
    detail["Z_VED_Full"] = detail["VED_Full"].map(VED_Z).fillna(0.0)
    detail["Z_Final_Corrected"] = np.maximum(
        pd.to_numeric(detail["Z ABC-XYZ"], errors="coerce").fillna(0),
        detail["Z_VED_Full"],
    )

    F = pd.to_numeric(detail["LT ашигласан (сар)"], errors="coerce").fillna(0).clip(lower=0)
    G = pd.to_numeric(detail["Lead Time STDEV.S"], errors="coerce").fillna(0).clip(lower=0)
    I = pd.to_numeric(detail["Сарын дундаж тоо"], errors="coerce").fillna(0).clip(lower=0)
    J = pd.to_numeric(detail["Сарын STDEV.S"], errors="coerce").fillna(0).clip(lower=0)
    raw_rop = F * I + detail["Z_Final_Corrected"] * np.sqrt(F * J**2 + I**2 * G**2)
    detail["ROP_Corrected"] = np.ceil(np.maximum(raw_rop, 0)).astype(float)

    branches = sorted(detail["Салбар"].dropna().astype(str).unique())
    dim_branch = pd.DataFrame({"BranchName": ["NETWORK"] + branches})
    dim_branch["Branch_ID"] = ["NETWORK"] + [f"B{idx:03d}" for idx in range(1, len(branches) + 1)]
    dim_branch["BranchGroup"] = dim_branch["BranchName"].map(branch_group)
    dim_branch["IsNetwork"] = dim_branch["Branch_ID"].eq("NETWORK")
    branch_map = dim_branch.set_index("BranchName")["Branch_ID"].to_dict()

    fact = pd.DataFrame({
        "SKU_ID": detail["SKU ID"].astype(int),
        "Branch_ID": detail["Салбар"].map(branch_map),
        "BranchName": detail["Салбар"].astype(str),
        "ROP": detail["ROP_Corrected"],
        "AvgMonthlyDemand_Branch": I,
        "DemandSD": J,
        "LeadTimeMean": F,
        "LeadTimeSD": G,
        "Sales7M": pd.to_numeric(detail["7 сарын борлуулалтын дүн"], errors="coerce").fillna(0),
        "ABC": detail["ABC"].fillna(""),
        "XYZ": detail["XYZ"].fillna(""),
        "ABC_XYZ": detail["ABC-XYZ"].fillna(""),
        "Z_VED": detail["Z_VED_Full"],
        "Z_ABCXYZ": pd.to_numeric(detail["Z ABC-XYZ"], errors="coerce").fillna(0),
        "Z_Final": detail["Z_Final_Corrected"],
        "VED": detail["VED_Full"],
        "Category": detail["Категори"].map(clean_category),
    })
    fact = fact[fact["Branch_ID"].notna()].reset_index(drop=True)
    fact["HasROP"] = fact["ROP"] > 0
    return fact, dim_branch


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the ROP advanced dashboard star schema from the source workbooks.")
    parser.add_argument("--rop", default="/mnt/data/ROP тооцоолол.xlsb")
    parser.add_argument("--v1", default="/mnt/data/ROP_үлдэгдэл_автомат_дашбоард_v1.xlsx")
    parser.add_argument("--inventory", default="/mnt/data/Үлд 20260813 агуулахгүй.xlsx")
    parser.add_argument("--snapshot-date", default="2026-08-13")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--excess-threshold", type=float, default=2.0)
    parser.add_argument("--categories", type=Path, default=None, help="CSV: product name, category")
    parser.add_argument("--powerbi", action="store_true", help="Also export readable CSVs to powerbi_data/")
    args = parser.parse_args()

    data_dir = Path(args.output) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    dim_sku = load_rop_master(args.v1)
    alias = load_alias_mapping(args.v1)
    category_stats = None
    if args.categories is not None:
        category_by_sku, category_review, category_stats = load_category_map(args.categories, dim_sku, alias)
        dim_sku = apply_category_map(dim_sku, category_by_sku)
        category_review.to_csv(data_dir / "category_review.csv", index=False, encoding="utf-8-sig")
    fact_branch_rop, dim_branch = load_branch_rop(args.rop, dim_sku)
    inventory, inventory_meta = parse_inventory(args.inventory)
    inventory["SnapshotDate"] = pd.to_datetime(inventory["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(args.snapshot_date))
    inventory["SourceFile"] = Path(args.inventory).name
    mapped, mapping_review, mapping_stats = map_inventory(inventory, alias, dim_sku, dim_branch)
    status = build_status_snapshot(dim_sku, mapped, fact_branch_rop, args.snapshot_date, args.excess_threshold)

    fact_inventory = mapped.copy()
    fact_inventory["SnapshotDate"] = pd.to_datetime(fact_inventory["SnapshotDate"], errors="coerce").fillna(pd.Timestamp(args.snapshot_date)).dt.date.astype(str)
    fact_inventory["DateKey"] = pd.to_datetime(fact_inventory["SnapshotDate"]).dt.strftime("%Y%m%d").astype("int32")
    fact_inventory["Scope"] = mapping_stats["scope"]
    fact_inventory["SourceFile"] = Path(args.inventory).name

    tables = status_tables(status, dim_branch, fact_branch_rop, fact_inventory, data_dir=data_dir)
    frames = {
        **tables,
        "dim_sku": dim_sku,
        **build_static_dims(dim_sku),
        "fact_branch_rop": to_fact_branch_rop(fact_branch_rop),
        "fact_inventory_snapshot": fact_inventory,
        "alias_mapping": alias,
        "mapping_review": mapping_review,
        "data_dictionary": DATA_DICTIONARY,
    }
    write_tables(frames, data_dir=data_dir, powerbi=args.powerbi)

    metrics = {
        "snapshot_date": args.snapshot_date,
        "source_files": {
            "rop": Path(args.rop).name,
            "master": Path(args.v1).name,
            "inventory": Path(args.inventory).name,
        },
        "inventory_parser": inventory_meta,
        "mapping": mapping_stats,
        "sku_count": int(len(dim_sku)),
        "branch_count": int((~tables["dim_branch"]["IsNetwork"]).sum()),
        "branch_rop_rows": int(len(frames["fact_branch_rop"])),
        "network_rop_total": float(dim_sku["ROP_Used"].sum()),
        "corrected_branch_rop_total": float(frames["fact_branch_rop"]["ROP"].sum()),
        "status_counts": {str(k): int(v) for k, v in status["Status"].value_counts().to_dict().items()},
        "critical_ve_sku": int(status["IsCritical"].sum()),
        "rop_gap_total": float(status["ROPGap"].sum(skipna=True)),
        "on_hand_total_matched": float(status["OnHand"].sum(skipna=True)),
        "missing_inventory_sku": int((~status["HasInventory"]).sum()),
        "missing_rop_sku": int((~dim_sku["HasROP"]).sum()),
        "low_confidence_rop_sku": int((dim_sku["ROP_Confidence"] == "Бага").sum()),
        "formula": "CEILING(AvgDemand*MeanLT + MAX(Z_VED,Z_ABCXYZ)*SQRT(MeanLT*DemandSD^2 + AvgDemand^2*LTSD^2),1)",
        "excess_threshold": args.excess_threshold,
    }
    if category_stats is not None:
        metrics["categories"] = {"source_file": Path(args.categories).name, **category_stats}
    save_json(data_dir / "metadata.json", metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
