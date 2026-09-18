import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import build_status_snapshot  # noqa: E402
from queries import DashboardQuery  # noqa: E402
from warehouse import (  # noqa: E402
    branch_rop_wide,
    build_dim_date,
    build_static_dims,
    ensure_branches,
    to_fact_branch_rop,
    to_fact_status,
)


@pytest.fixture
def dim_sku() -> pd.DataFrame:
    return pd.DataFrame({
        "SKU_ID": [1, 2, 3],
        "SKU_Name": ["Тобрекс дусал", "Парацетамол 500мг", "Витамин C"],
        "MasterKey": ["ТОБРЕКСДУСАЛ", "ПАРАЦЕТАМОЛ500МГ", "ВИТАМИНC"],
        "Category": ["Импорт", "Дотоод", "Дагалдах"],
        "VED": ["V", "E", "D"],
        "LeadTime_Months": [1.0, 0.5, 2.0],
        "LeadTime_Status": ["", "", ""],
        "Sales_7M": [10.0, 20.0, 30.0],
        "AvgMonthlyDemand": [5.0, 8.0, 2.0],
        "ROP_Used": [30.0, 20.0, 4.0],
        "ROP_Source": ["Зассан дэлгэрэнгүй томьёо"] * 3,
        "ROP_Confidence": ["Өндөр", "Өндөр", "Бага"],
        "HasROP": [True, True, True],
    })


@pytest.fixture
def dim_branch() -> pd.DataFrame:
    return ensure_branches(pd.DataFrame({
        "BranchName": ["NETWORK", "АФ Төв э/сан", "АФ Зүүн э/сан", "Мед_Премиум э/сан"],
        "Branch_ID": ["NETWORK", "B001", "B002", "B003"],
    }))


@pytest.fixture
def branch_rop() -> pd.DataFrame:
    """Wide ROP rows as the ROP workbook produces them, incl. one duplicate
    SKU x branch row with ROP 0 and one excluded branch."""
    rows = [
        (1, "B001", "АФ Төв э/сан", 10.0, "V"),
        (2, "B001", "АФ Төв э/сан", 20.0, "E"),
        (3, "B001", "АФ Төв э/сан", 0.0, "D"),
        (1, "B002", "АФ Зүүн э/сан", 10.0, "V"),
        (2, "B002", "АФ Зүүн э/сан", 5.0, "E"),
        (2, "B002", "АФ Зүүн э/сан", 0.0, "E"),  # duplicate key
        (3, "B002", "АФ Зүүн э/сан", 4.0, "D"),
        (1, "B003", "Мед_Премиум э/сан", 7.0, "V"),  # excluded branch
    ]
    df = pd.DataFrame(rows, columns=["SKU_ID", "Branch_ID", "BranchName", "ROP", "VED"])
    df["Category"] = "x"
    df["HasROP"] = df["ROP"] > 0
    return df


@pytest.fixture
def inventory() -> pd.DataFrame:
    rows = [
        # SKU, branch id, branch name, on hand, supplier
        (1, "B001", "АФ Төв э/сан", 0.0, "Нийлүүлэгч А"),   # stockout
        (2, "B001", "АФ Төв э/сан", 25.0, "Нийлүүлэгч Б"),  # normal (20 <= 25 < 40)
        (2, "B001", "АФ Төв э/сан", 0.0, "Нийлүүлэгч А"),   # second line, same SKU x branch
        (1, "B002", "АФ Зүүн э/сан", 4.0, ""),              # below ROP
        (2, "B002", "АФ Зүүн э/сан", 50.0, "Нийлүүлэгч Б"), # excess (>= 2 x 5)
        (1, "B003", "Мед_Премиум э/сан", 1.0, ""),          # excluded branch
    ]
    df = pd.DataFrame(rows, columns=["SKU_ID", "Branch_ID", "BranchName", "OnHand", "Supplier"])
    df["SKU_ID"] = df["SKU_ID"].astype("Int64")
    df["OnOrder"] = 0.0
    df["Backorder"] = 0.0
    df["InventoryPosition"] = df["OnHand"]
    df["Manufacturer"] = ""
    df["InventoryLineID"] = np.arange(1, len(df) + 1)
    return df


@pytest.fixture
def status_wide(dim_sku, dim_branch, branch_rop, inventory) -> pd.DataFrame:
    rop = branch_rop_wide(to_fact_branch_rop(branch_rop), dim_branch, dim_sku)
    return build_status_snapshot(dim_sku, inventory, rop, "2026-09-16", excess_threshold=2.0)


@pytest.fixture
def db(dim_sku, dim_branch, branch_rop, status_wide) -> DashboardQuery:
    fact = to_fact_status(status_wide)
    earlier = fact.copy()
    earlier["DateKey"] = 20260911
    earlier["Status"] = "Хэвийн"
    history = pd.concat([earlier, fact], ignore_index=True)
    con = duckdb.connect()
    tables = {
        "dim_sku": dim_sku,
        "dim_branch": dim_branch,
        "dim_date": build_dim_date([20260911, 20260916]),
        **build_static_dims(dim_sku),
        "fact_branch_rop": to_fact_branch_rop(branch_rop),
        "fact_sku_status": fact,
        "fact_status_history": history,
        "alias_mapping": pd.DataFrame({"NormalizedKey": ["A"], "SKU_ID": [1]}),
    }
    for name, frame in tables.items():
        con.register("_t", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _t")
        con.unregister("_t")
    return DashboardQuery(con)
