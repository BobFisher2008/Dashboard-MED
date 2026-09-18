import numpy as np
import pandas as pd
import pytest

from common import (
    apply_category_map,
    apply_missing_as_zero,
    classify_status,
    classify_status_vectorized,
    clean_category,
    load_category_map,
)
from metrics import MetricsCalculator
from queries import Filters
from warehouse import to_fact_status


@pytest.mark.parametrize("on_hand, rop, has_data, expected", [
    (None, 10, True, "Өгөгдөл алга"),
    (5, 10, False, "Өгөгдөл алга"),
    (5, 0, True, "ROP байхгүй"),
    (5, None, True, "ROP байхгүй"),
    (0, 10, True, "Тасарсан"),
    (-2, 10, True, "Тасарсан"),
    (9.99, 10, True, "ROP-оос доош"),
    (10, 10, True, "Хэвийн"),
    (19.99, 10, True, "Хэвийн"),
    (20, 10, True, "Илүүдэл"),
])
def test_classify_status_scalar_and_vectorized_agree(on_hand, rop, has_data, expected):
    assert classify_status(on_hand, rop, 2.0, has_data) == expected
    vec = classify_status_vectorized(
        pd.Series([on_hand], dtype="float64"), pd.Series([rop], dtype="float64"), pd.Series([has_data]), 2.0
    )
    assert vec[0] == expected


def test_clean_category_canonical_labels():
    assert clean_category("Экс") == "Эксклюзив"
    assert clean_category("Эксклюзив") == "Эксклюзив"
    assert clean_category(None) == "Бусад"
    assert clean_category("0x17") == "Бусад"
    assert clean_category("Брэнд") == "Брэнд"


def test_status_snapshot_to_fact_round_trip(status_wide):
    fact = to_fact_status(status_wide)
    by_key = fact.set_index(["SKU_ID", "Branch_ID"])["Status"].to_dict()
    assert by_key == {
        (1, "B001"): "Тасарсан",
        (2, "B001"): "Хэвийн",
        (3, "B001"): "Өгөгдөл алга",
        (1, "B002"): "ROP-оос доош",
        (2, "B002"): "Илүүдэл",
        (3, "B002"): "Өгөгдөл алга",
    }
    # excluded branch dropped, one row per key, dimension attributes not copied
    assert "B003" not in set(fact["Branch_ID"])
    assert not fact.duplicated(["SKU_ID", "Branch_ID", "DateKey"]).any()
    assert set(fact["DateKey"]) == {20260916}
    assert not {"SKU_Name", "Category", "BranchName"} & set(fact.columns)
    # two inventory lines for SKU 2 at B001 are summed and their suppliers joined
    row = fact[(fact["SKU_ID"] == 2) & (fact["Branch_ID"] == "B001")].iloc[0]
    assert row["OnHand"] == 25 and row["InventoryLines"] == 2
    assert row["Supplier"] == "Нийлүүлэгч А; Нийлүүлэгч Б"
    # the duplicate ROP row (ROP 0) lost against the real one
    assert fact.set_index(["SKU_ID", "Branch_ID"]).loc[(2, "B002"), "ROP"] == 5


def test_to_fact_status_is_idempotent(status_wide):
    once = to_fact_status(status_wide)
    twice = to_fact_status(once)
    pd.testing.assert_frame_equal(once, twice)


def test_sql_kpis_match_pandas_reference(db, status_wide):
    expected = MetricsCalculator.calculate_kpis(to_fact_status(status_wide))
    assert db.kpis(Filters()) == pytest.approx(expected)
    assert expected == {"sku_count": 3, "inventory_position": 79, "total_rop": 49, "rop_gap": 16, "critical": 2}


def test_filters_are_applied_in_sql(db):
    assert db.kpis(Filters(veds=("V",)))["sku_count"] == 1
    assert db.kpis(Filters(categories=("Дотоод",)))["total_rop"] == 25
    assert db.kpis(Filters(branch="АФ Зүүн э/сан"))["inventory_position"] == 54
    assert db.kpis(Filters(critical_only=True))["rop_gap"] == 16
    assert db.kpis(Filters(search="ТОБРЕКС"))["sku_count"] == 1
    assert db.kpis(Filters(search="нийлүүлэгч б"))["inventory_position"] == 75
    assert db.kpis(Filters(min_gap=7))["rop_gap"] == 10
    counts = db.status_counts(Filters())
    assert counts["Өгөгдөл алга"] == 2 and counts["Тасарсан"] == 1


def test_filters_where_is_parameterized():
    sql, params = Filters(statuses=("Хэвийн",), search="x'; DROP TABLE dim_sku; --", branch="B").where()
    assert "DROP" not in sql
    assert params[0] == "Хэвийн" and params[1] == "B"
    assert Filters().where() == ("TRUE", [])


def test_query_cache_is_invalidated_on_replace(db):
    before = db.kpis(Filters())
    fact = db.table_frame("fact_sku_status")
    fact["ROP"] = fact["ROP"] * 2
    db.replace_table("fact_sku_status", fact)
    assert db.kpis(Filters())["total_rop"] == before["total_rop"] * 2


def test_coverage(db):
    cov = MetricsCalculator.finalize_coverage(db.coverage(Filters())).set_index(["BranchName", "Category"])
    b1 = cov.loc[("АФ Төв э/сан", "Импорт")]
    assert b1["EligibleSKU"] == 1 and b1["CoveredSKU"] == 0 and b1["Coverage"] == 0
    assert cov.loc[("АФ Төв э/сан", "Дотоод"), "Coverage"] == 1
    assert np.isnan(cov.loc[("АФ Төв э/сан", "Дагалдах"), "Coverage"])  # no ROP -> not eligible
    assert cov.loc[("АФ Зүүн э/сан", "Импорт"), "Coverage"] == pytest.approx(0.4)
    assert cov.loc[("АФ Зүүн э/сан", "Импорт"), "Gap"] == 6


def test_trend_and_movement(db):
    wide = MetricsCalculator.trend_table(db.trend(Filters(), True))
    assert wide["DateKey"].tolist() == [20260911, 20260916]
    assert wide["Total"].tolist() == [6, 6]
    assert wide.iloc[1]["Тасарсан"] == 1 and wide.iloc[0]["Хэвийн"] == 6
    summary = db.movement_summary(Filters(), 20260911, 20260916)
    # Хэвийн -> Тасарсан / ROP-оос доош / Өгөгдөл алга x2 are worse; Илүүдэл has equal severity
    assert summary == {"worse": 4, "better": 0, "changed": 5, "compared": 6}
    moved = db.movement(Filters(), 20260911, 20260916)
    assert moved.iloc[0]["StatusTo"] == "Тасарсан" and moved.iloc[0]["Direction"] == "Муудсан"


def test_apply_missing_as_zero(status_wide):
    status = apply_missing_as_zero(status_wide)
    row = status[status["DataMatch"] == "Missing treated as zero"].set_index(["SKU_ID", "Branch_ID"])
    assert len(row) == 2
    assert row.loc[(3, "B001"), "Status"] == "ROP байхгүй"
    assert row.loc[(3, "B002"), "Status"] == "Тасарсан" and row.loc[(3, "B002"), "ROPGap"] == 4
    assert status["HasInventory"].all()


def test_load_category_map(tmp_path, dim_sku):
    csv = tmp_path / "cats.csv"
    csv.write_text(
        "Эм барааны нэр,Ангилал\n"
        "Тобрекс дусал,Брэнд\n"          # master-key match
        "Тобрекс-дусал,Брэнд\n"          # same key, same category
        "Парацетамол 500 мг,Экс\n"       # alias match; Экс -> Эксклюзив
        "Витамин C,Дотоод\n"
        "Витамин C,Импорт\n"
        "Витамин  C,Импорт\n"            # conflict: Импорт wins 2-1
        "Үл мэдэгдэх бараа,Дотоод\n",    # unmatched
        encoding="utf-8-sig",
    )
    master = dim_sku.assign(MasterKey=["ТОБРЕКСДУСАЛ", "PARA", "ВИТАМИНC"])
    alias = pd.DataFrame({"NormalizedKey": ["ПАРАЦЕТАМОЛ500МГ"], "SKU_ID": [2]})
    category, review, stats = load_category_map(csv, master, alias)
    assert category.to_dict() == {1: "Брэнд", 2: "Эксклюзив", 3: "Импорт"}
    assert stats["sku_conflicts"] == 1 and stats["unmatched_rows"] == 1 and stats["sku_uncovered"] == 0
    assert set(review["Issue"]) == {"Эх файлын нэр SKU-тэй таарсангүй", "Олон категори — давамгайг сонгов"}
    assert apply_category_map(dim_sku, category)["Category"].tolist() == ["Брэнд", "Эксклюзив", "Импорт"]
