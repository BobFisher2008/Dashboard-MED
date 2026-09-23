"""Dashboard rules: expiry column aliases, rows without average sales, the
«ABC XYZ.xlsx» class fill, «Салбарын жагсаалт» and the Хугацаат tab queries."""
import numpy as np
import pandas as pd
import pytest

from common import load_abc_xyz_file, load_branch_list, parse_inventory
from expiry import build_batch_expiry
from queries import CLASS_SOURCE_FILE, CLASS_SOURCE_ROP, UNKNOWN_CLASS, Filters
from warehouse import to_fact_branch_rop


# --------------------------------------------------------------------------
# Expiry column aliases
# --------------------------------------------------------------------------
@pytest.mark.parametrize("header", ["Сери.Хүртэл хүчинтэй", "Огноо", "Date", "Expiry Date", "Хугацаа"])
def test_every_alias_is_read_as_the_expiry_date(tmp_path, header):
    csv = tmp_path / "Үлд 0921.csv"
    csv.write_text(f"Агуулах,Бараа материал,{header},Эцсийн үлдэгдэл\nАФ Төв э/сан,Тобрекс дусал,11/30/2026,2\n",
                   encoding="utf-8-sig")
    out, meta = parse_inventory(csv)
    assert meta["column_map"]["expiry"] == header
    assert meta["column_map"]["snapshot_date"] is None  # «Огноо» / «Date» here is the expiry, not the snapshot
    assert out["ExpiryDate"].iloc[0] == pd.Timestamp("2026-11-30")


def test_the_specific_expiry_header_wins_over_a_generic_one(tmp_path):
    csv = tmp_path / "export.csv"
    csv.write_text("Агуулах,Бараа материал,Огноо,Сери.Хүртэл хүчинтэй,Эцсийн үлдэгдэл\n"
                   "АФ Төв э/сан,Тобрекс дусал,2026-09-21,11/30/2026,2\n", encoding="utf-8-sig")
    out, meta = parse_inventory(csv)
    assert meta["column_map"]["expiry"] == "Сери.Хүртэл хүчинтэй"
    assert meta["column_map"]["snapshot_date"] == "Огноо"
    assert out["ExpiryDate"].iloc[0] == pd.Timestamp("2026-11-30")


def test_a_snapshot_date_header_is_not_an_expiry_date(tmp_path):
    """The generic words match a whole header only: «Үлдэгдлийн огноо» stays the snapshot date."""
    csv = tmp_path / "export.csv"
    csv.write_text("Агуулах,Бараа материал,Үлдэгдлийн огноо,Эцсийн үлдэгдэл\nАФ Төв э/сан,Тобрекс дусал,2026-09-21,2\n",
                   encoding="utf-8-sig")
    out, meta = parse_inventory(csv)
    assert meta["column_map"]["expiry"] is None
    assert meta["column_map"]["snapshot_date"] == "Үлдэгдлийн огноо"
    assert out["ExpiryDate"].isna().all()


# --------------------------------------------------------------------------
# Rows without average sales
# --------------------------------------------------------------------------
def _without_sales(db):
    """SKU 3 at B002 gets 0 average sales; SKU 1 gets stock at B004, a branch
    the ROP workbook does not list (no fact_branch_rop row at all)."""
    rop = db.table_frame("fact_branch_rop")
    rop.loc[(rop["SKU_ID"] == 3) & (rop["Branch_ID"] == "B002"), "AvgMonthlyDemand_Branch"] = 0.0
    db.replace_table("fact_branch_rop", rop)
    fact = db.table_frame("fact_sku_status")
    stray = fact[(fact["SKU_ID"] == 1) & (fact["Branch_ID"] == "B001")].assign(
        Branch_ID="B004", ROP=0.0, ROPGap=0.0, OnHand=7.0, InventoryPosition=7.0, Status="ROP байхгүй", HasROP=False)
    db.replace_table("fact_sku_status", pd.concat([fact, stray], ignore_index=True))
    return db


def test_rows_without_sales_leave_every_status_count(db):
    before = db.status_counts(Filters())
    counts = _without_sales(db).status_counts(Filters())
    assert counts["Өгөгдөл алга"] == before["Өгөгдөл алга"] - 1  # SKU 3 at B002, 0 sales
    assert counts["ROP байхгүй"] == before["ROP байхгүй"]         # the B004 stock is not added
    assert counts.sum() == 5  # 6 rows minus SKU 3 at B002; the B004 row never enters
    assert db.kpis(Filters())["inventory_position"] == 79          # B004's 7 units are left out
    assert db.quality()["missing_inventory"] == 1
    assert "АФ Зүүн э/сан" in db.filter_options()["branches"]


def test_rows_without_sales_leave_the_history_and_the_abc_tab(db):
    _without_sales(db)
    history = db.df("SELECT count(*) AS n FROM v_history")["n"].iloc[0]
    assert history == 10  # 12 rows over 2 dates minus SKU 3 at B002 on both
    assert not ((db.class_items(Filters())["SKU_ID"] == 3) & (db.class_items(Filters())["BranchName"] == "АФ Зүүн э/сан")).any()


def test_network_rows_use_the_skus_own_sales(db, dim_sku):
    dim = dim_sku.assign(AvgMonthlyDemand=[5.0, 0.0, np.nan])
    db.replace_table("dim_sku", dim)
    network = db.df("SELECT SKU_ID FROM v_has_sales WHERE Branch_ID = 'NETWORK'")["SKU_ID"].tolist()
    assert network == [1]
    assert db.quality()["missing_rop_sku"] == 0  # SKUs without sales are not "missing ROP"


# --------------------------------------------------------------------------
# «ABC XYZ.xlsx»
# --------------------------------------------------------------------------
def test_load_abc_xyz_file(tmp_path, dim_sku, dim_branch):
    path = tmp_path / "ABC XYZ.xlsx"
    pd.DataFrame({
        "Нэр төрөл": ["Тобрекс дусал", "Парацетамол 500мг", "Үл мэдэгдэх бараа"],
        "АФ Төв э/сан": ["AZ", "#", "AX"],
        "АФ  Зүүн э/сан": ["ВХ", None, "AX"],       # double space; Cyrillic В / Х
        "Мед_Премиум э/сан": ["AX", "AX", "AX"],    # excluded branch
        "Шинэ салбар": ["CY", "CY", "CY"],          # not in dim_branch
    }).to_excel(path, index=False)
    table, stats = load_abc_xyz_file(path, dim_sku, dim_branch)
    assert table.sort_values(["SKU_ID", "Branch_ID"]).values.tolist() == [[1, "B001", "A", "Z"], [1, "B002", "B", "X"]]
    assert stats["branches_matched"] == 2 and stats["branches_unmatched"] == ["Шинэ салбар"]
    assert stats["matched_names"] == 2 and stats["invalid_cells"] == 1


def test_the_file_fills_only_missing_classes(db):
    """B002 SKU 3 carries no class in the shared fixture; B001 SKU 1 has none either."""
    rop = db.table_frame("fact_branch_rop").assign(ABC="", XYZ="")
    rop.loc[rop["SKU_ID"] == 2, ["ABC", "XYZ"]] = ["B", "Y"]
    db.replace_table("fact_branch_rop", rop)
    db.replace_table("fact_abc_xyz_file", pd.DataFrame({
        "SKU_ID": [1, 2], "Branch_ID": ["B001", "B001"], "ABC": ["A", "C"], "XYZ": ["X", "Z"]}))
    items = db.class_items(Filters()).set_index(["SKU_ID", "BranchName"])
    assert items.loc[(1, "АФ Төв э/сан"), ["ABC", "XYZ", "ClassSource"]].tolist() == ["A", "X", CLASS_SOURCE_FILE]
    assert items.loc[(2, "АФ Төв э/сан"), ["ABC", "XYZ", "ClassSource"]].tolist() == ["B", "Y", CLASS_SOURCE_ROP]
    assert items.loc[(1, "АФ Зүүн э/сан"), "ABC"] == UNKNOWN_CLASS
    assert pd.isna(items.loc[(1, "АФ Зүүн э/сан"), "ClassSource"])


# --------------------------------------------------------------------------
# «Салбарын жагсаалт» and the Хугацаат tab
# --------------------------------------------------------------------------
def test_load_branch_list(tmp_path):
    quoted = tmp_path / "Салбарын жагсаалт.txt"
    quoted.write_text("  'АФ Төв э/сан',\r\n      'АФ Медвико-Медвико-2  э/сан',\r\n 'АФ Төв э/сан',\r\n'Эрдэнэт э/сан'",
                      encoding="utf-8")
    assert load_branch_list(quoted) == ["АФ Төв э/сан", "АФ Медвико-Медвико-2  э/сан", "Эрдэнэт э/сан"]
    plain = tmp_path / "plain.txt"
    plain.write_text("АФ Төв э/сан\n\nЭрдэнэт э/сан\n", encoding="utf-8")
    assert load_branch_list(plain) == ["АФ Төв э/сан", "Эрдэнэт э/сан"]


@pytest.fixture
def expiry_db(db, dim_sku, branch_rop, inventory):
    """Batches of the conftest inventory on 2026-09-16: one expired, one
    without a date, one with no stock, the rest ahead."""
    inv = inventory.assign(ExpiryDate=pd.to_datetime(
        ["2026-10-01", "2027-06-30", "2026-09-01", None, "2026-12-01", "2026-10-01"]))
    batches = build_batch_expiry(inv, dim_sku, to_fact_branch_rop(branch_rop), "2026-09-16")
    db.replace_table("fact_batch_expiry", batches)
    return db


def test_only_future_batches_with_stock_are_listed(expiry_db):
    items = expiry_db.expiry_items(Filters())
    # (1,B001) has 0 stock, (2,B001) 2026-09-01 is expired, (1,B002) has no date
    assert items[["SKU_ID", "BranchName", "Qty"]].values.tolist() == [
        [2, "АФ Зүүн э/сан", 50.0], [2, "АФ Төв э/сан", 25.0]]
    assert items["DTE"].is_monotonic_increasing
    k = expiry_db.expiry_kpis(Filters())
    assert (k["batches"], k["skus"], k["branches"], k["qty"]) == (2, 1, 2, 75)
    assert pd.Timestamp(k["nearest"]) == pd.Timestamp("2026-12-01")


def test_expiry_filters(expiry_db):
    assert expiry_db.expiry_kpis(Filters(), ("АФ Төв э/сан",))["qty"] == 25
    assert expiry_db.expiry_kpis(Filters(), (), ("WARNING",))["qty"] == 50   # DTE 76
    assert expiry_db.expiry_kpis(Filters(veds=("V",)))["batches"] == 0      # SKU 2 is E
    assert expiry_db.expiry_kpis(Filters(search="парацетамол"))["batches"] == 2
    # the ROP filters do not touch batches
    assert expiry_db.expiry_kpis(Filters(statuses=("Хэвийн",), min_gap=5, branch="x"))["batches"] == 2


def test_expiry_by_branch_and_months(expiry_db):
    by_branch = expiry_db.expiry_by_branch(Filters()).set_index("BranchName")
    assert by_branch.loc["АФ Зүүн э/сан", ["Batches", "WARNING", "Qty"]].tolist() == [1, 50, 50]
    assert by_branch.loc["АФ Төв э/сан", "WATCH"] == 0 and by_branch.loc["АФ Төв э/сан", "OK"] == 25
    months = expiry_db.expiry_months(Filters())
    assert months["Qty"].sum() == 75


def test_no_batch_table_means_an_empty_tab(db):
    assert db.expiry_items(Filters()).empty
    assert db.expiry_kpis(Filters())["batches"] == 0


def test_active_branches_limit_every_rop_view(db):
    """«Салбарын жагсаалт»: only the listed branches are reported; () = all."""
    everything = db.kpis(Filters())
    db.set_active_branches(("АФ Төв э/сан",))
    assert db.filter_options()["branches"] == ["АФ Төв э/сан"]
    assert db.kpis(Filters())["inventory_position"] == 25   # B002's stock is out
    assert db.status_counts(Filters()).sum() == 3
    assert set(db.df("SELECT DISTINCT BranchName FROM v_history")["BranchName"]) == {"АФ Төв э/сан"}
    assert db.branch_exposure()["Branch"].tolist() == ["АФ Төв э/сан"]
    db.set_active_branches(())
    assert db.kpis(Filters()) == everything
