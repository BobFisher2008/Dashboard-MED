"""The VED file override and the ABC / XYZ tab queries."""
import pandas as pd
import pytest

from common import (
    VED_FILE_SOURCE,
    VED_MASTER_SOURCE,
    apply_ved_map,
    build_status_snapshot,
    clean_ved,
    load_ved_map,
    match_rop_skus,
)
from metrics import MetricsCalculator
from queries import CLASS_ORDER, TOTAL, UNKNOWN_CLASS, Filters
from warehouse import (
    apply_sku_ved,
    branch_rop_wide,
    integrity_checks,
    to_fact_branch_rop,
    to_fact_status,
    ved_differences,
)

KEYS = ["SKU_ID", "Branch_ID"]
VED_COLUMNS = ["VED", "WeightedGap", "PriorityScore", "IsCritical"]


@pytest.fixture
def branch_rop(branch_rop):
    """The shared ROP rows plus the workbook's ABC / XYZ / sales columns.
    Row 7 (SKU 3 at B002) carries the workbook's '#' error value."""
    return branch_rop.assign(
        ABC=["A", "B", "C", "A", "B", "B", "#", "A"],
        XYZ=["X", "Y", "Z", "Y", "Z", "Z", None, "X"],
        Sales7M=[100.0, 50.0, 10.0, 80.0, 40.0, 0.0, 0.0, 5.0],
    )


# --------------------------------------------------------------------------
# Loading the VED file
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("V", "V"), (" e ", "E"), ("d", "D"),
    ("Д", "D"), ("Е", "E"), ("В", "V"),  # Cyrillic typed for D / E / V
    ("X", None), ("", None), (None, None), ("VE", None),
])
def test_clean_ved(raw, expected):
    assert clean_ved(raw) == expected


def test_load_ved_map(tmp_path, dim_sku):
    csv = tmp_path / "ved.csv"
    csv.write_text(
        "Барааны нэр,Барааны ангилал,VED\n"
        "Тобрекс дусал,Эм,E\n"           # the SKU's own name ...
        "Тобрекс-дусал,Эм,V\n"           # ... beats a more critical VED on the same key
        "Парацетамол 500 мг,Эм,e\n"      # alias match, lower case
        "Витамин-C,Хүнс,Д\n"             # Cyrillic D, master key
        "Витамин  C,Хүнс,В\n"            # Cyrillic V on the same key: equal match, V beats D
        "Витамин C,Хүнс,X\n"             # not a VED class: reported, not applied
        "Үл мэдэгдэх бараа,Бусад,D\n",   # unmatched
        encoding="utf-8-sig",
    )
    master = dim_sku.assign(MasterKey=["ТОБРЕКСДУСАЛ", "PARA", "ВИТАМИНC"])
    alias = pd.DataFrame({"NormalizedKey": ["ПАРАЦЕТАМОЛ500МГ"], "SKU_ID": [2]})
    ved, review, stats = load_ved_map(csv, master, alias)

    assert ved.to_dict() == {1: "E", 2: "E", 3: "V"}
    assert stats == {
        "source_rows": 7, "matched_rows": 6, "unmatched_rows": 1, "invalid_rows": 1,
        "sku_covered": 3, "sku_uncovered": 0, "sku_conflicts": 2,
    }
    issues = review.groupby("Issue")["SKU_ID"].apply(lambda s: sorted(s.dropna().astype(int)))
    assert issues["Олон VED — SKU-ийн өөрийн нэртэйг, үгүй бол хамгийн чухлыг (V > E > D) сонгов"] == [1, 1, 3, 3]
    assert issues["VED утга V / E / D биш — хэрэглээгүй"] == [3]
    assert review.loc[review["Issue"] == "VED файлын нэр SKU-тэй таарсангүй", "SourceName"].tolist() == ["Үл мэдэгдэх бараа"]


def test_load_ved_map_keeps_skus_that_share_a_name_apart(tmp_path, dim_sku):
    """Two SKUs whose names differ only by N / № share a normalized key; each
    takes the VED listed under its own name."""
    twins = pd.DataFrame({
        "SKU_ID": [10, 11], "SKU_Name": ["Бумба N6", "Бумба №6"], "MasterKey": ["БУМБАN6", "БУМБАN6"], "VED": ["D", "D"],
    })
    csv = tmp_path / "ved.csv"
    csv.write_text("Барааны нэр,VED\nБумба N6,E\nБумба №6,D\n", encoding="utf-8-sig")
    ved, _, _ = load_ved_map(csv, twins)
    assert ved.to_dict() == {10: "E", 11: "D"}
    # listed under one of the names only: both SKUs still take it
    csv.write_text("Барааны нэр,VED\nБумба №6,V\n", encoding="utf-8-sig")
    ved, _, stats = load_ved_map(csv, twins)
    assert ved.to_dict() == {10: "V", 11: "V"} and stats["sku_uncovered"] == 0


def test_load_ved_map_needs_a_ved_column(tmp_path, dim_sku):
    csv = tmp_path / "ved.csv"
    csv.write_text("Барааны нэр,Ангилал\nТобрекс дусал,Эм\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="VED багана"):
        load_ved_map(csv, dim_sku)


def test_apply_ved_map_marks_the_listed_skus(dim_sku):
    out = apply_ved_map(dim_sku, pd.Series({2: "V"}))
    assert out["VED"].tolist() == ["V", "V", "D"]
    assert out["VED_Source"].tolist() == [VED_MASTER_SOURCE, VED_FILE_SOURCE, VED_MASTER_SOURCE]
    # a later file that no longer lists SKU 2 leaves its VED and source alone
    again = apply_ved_map(out, pd.Series({3: "E"}))
    assert again["VED"].tolist() == ["V", "V", "E"]
    assert again["VED_Source"].tolist() == [VED_MASTER_SOURCE, VED_FILE_SOURCE, VED_FILE_SOURCE]


# --------------------------------------------------------------------------
# Matching ROP workbook rows to SKUs
# --------------------------------------------------------------------------
def test_match_rop_skus_goes_by_name_not_workbook_id(dim_sku):
    """The workbook's «SKU ID» is its own numbering; the product name decides
    the SKU. Twins that share a key each keep their own name's rows."""
    master = pd.concat([dim_sku, pd.DataFrame({
        "SKU_ID": [10, 11], "SKU_Name": ["Бумба N6", "Бумба №6"], "MasterKey": ["БУМБАN6", "БУМБАN6"],
    })], ignore_index=True)
    alias = pd.DataFrame({"NormalizedKey": ["PARACETAMOL"], "SKU_ID": [2]})
    detail = pd.DataFrame({
        "SKU ID": [2, 2, 5, 9, 12, 7, 8],
        "Нэр төрөл": [
            "Тобрекс дусал",        # workbook ID 2 is another product: goes to SKU 1
            "Парацетамол 500мг",    # same ID on both sides
            "Витамин-C",            # master key only
            "Бумба №6 ",            # twin: its own name wins over the shared key; trailing space
            "Бумба N6",
            "Paracetamol",          # alias
            "Үл мэдэгдэх бараа",    # unmatched: dropped
        ],
        "Салбар": ["АФ Төв э/сан"] * 7,
    })
    out, stats = match_rop_skus(detail, master, alias)

    assert out["SKU_ID"].tolist() == [1, 2, 3, 11, 10, 2]
    assert out["WorkbookSKU_ID"].tolist() == [2, 2, 5, 9, 12, 7]
    assert "SourceName" not in out
    assert stats == {
        "rows": 7, "matched_rows": 6, "unmatched_rows": 1,
        "match_type_rows": {"Exact name": 4, "Master key": 1, "Alias": 1},
        "rows_with_other_workbook_id": 5, "sku_with_other_workbook_id": 5,
        "unmatched_names": ["Үл мэдэгдэх бараа"],
    }


# --------------------------------------------------------------------------
# Applying it to the facts
# --------------------------------------------------------------------------
# SKU 1: V -> D (stocked out at B001, below ROP at B002, so its weighted gap
# and criticality change too), SKU 3: D -> E; SKU 2 is not in the file.
NEW_VED = {1: "D", 3: "E"}


def test_patched_status_equals_a_rebuild_with_the_new_ved(dim_sku, dim_branch, branch_rop, inventory, status_wide):
    """Patching the VED of an existing fact must give exactly what building
    the snapshot from scratch with the new VED gives."""
    new_dim = apply_ved_map(dim_sku, pd.Series(NEW_VED))
    patched = apply_sku_ved(to_fact_status(status_wide), new_dim)

    new_rop = branch_rop.assign(VED=branch_rop["SKU_ID"].map({2: "E", **NEW_VED}))
    rebuilt = to_fact_status(build_status_snapshot(
        new_dim, inventory, branch_rop_wide(to_fact_branch_rop(new_rop), dim_branch, new_dim), "2026-09-16"))

    pd.testing.assert_frame_equal(
        patched.sort_values(KEYS).reset_index(drop=True)[KEYS + VED_COLUMNS],
        rebuilt.sort_values(KEYS).reset_index(drop=True)[KEYS + VED_COLUMNS],
        check_dtype=False,
    )
    row = patched.set_index(KEYS)
    # SKU 1 at B001: stocked out, gap 10; as D it weighs 1 and is no longer critical
    assert row.loc[(1, "B001"), "WeightedGap"] == 10
    assert row.loc[(1, "B001"), "PriorityScore"] == pytest.approx(100 + 1 * 10 + 10 / 100)
    assert not row.loc[(1, "B001"), "IsCritical"]
    # SKU 3 at B002 had no inventory: Өгөгдөл алга 55 + E 2 x 10 + low-confidence 5
    assert row.loc[(3, "B002"), "PriorityScore"] == pytest.approx(80)


def test_apply_sku_ved_leaves_other_rows_alone(dim_sku, status_wide):
    fact = to_fact_status(status_wide)
    patched = apply_sku_ved(fact, apply_ved_map(dim_sku, pd.Series(NEW_VED)))
    untouched = fact["SKU_ID"] == 2
    pd.testing.assert_frame_equal(patched[untouched], fact[untouched])
    # idempotent, and a dim_sku without VED_Source changes nothing
    again = apply_sku_ved(patched, apply_ved_map(dim_sku, pd.Series(NEW_VED)))
    pd.testing.assert_frame_equal(again, patched)
    assert apply_sku_ved(fact, dim_sku) is fact


def test_branch_rop_keeps_the_workbook_ved(dim_sku, branch_rop):
    rop = to_fact_branch_rop(branch_rop)
    assert rop["VED_ROP"].tolist() == rop["VED"].tolist()
    new_dim = apply_ved_map(dim_sku, pd.Series(NEW_VED))
    patched = apply_sku_ved(rop, new_dim)
    by_sku = patched.groupby("SKU_ID")[["VED", "VED_ROP"]].first()
    assert by_sku.loc[1].tolist() == ["D", "V"] and by_sku.loc[3].tolist() == ["E", "D"]
    assert by_sku.loc[2].tolist() == ["E", "E"]
    assert patched["ROP"].tolist() == rop["ROP"].tolist()
    # re-shaping keeps VED_ROP instead of resetting it to the new VED
    assert to_fact_branch_rop(patched)["VED_ROP"].tolist() == rop["VED_ROP"].tolist()

    diff = ved_differences(patched, new_dim).set_index("SKU_ID")
    assert diff[["VED", "PreviousVED", "Rows"]].to_dict("index") == {
        1: {"VED": "D", "PreviousVED": "V", "Rows": 2},
        3: {"VED": "E", "PreviousVED": "D", "Rows": 2},
    }


def test_integrity_check_catches_a_fact_that_missed_the_ved_file(db, dim_sku):
    assert integrity_checks(db.con)["all_ok"]
    new_dim = apply_ved_map(dim_sku, pd.Series(NEW_VED))
    db.replace_table("dim_sku", new_dim)
    checks = integrity_checks(db.con)
    for table in ["fact_branch_rop", "fact_sku_status", "fact_status_history"]:
        assert checks[f"{table}_invalid_ved"] > 0, table
        db.replace_table(table, apply_sku_ved(db.table_frame(table), new_dim))
    assert integrity_checks(db.con)["all_ok"]


# --------------------------------------------------------------------------
# ABC / XYZ tab
# --------------------------------------------------------------------------
def _classes(db, column, **filters):
    return MetricsCalculator.class_rollup(db.class_breakdown(Filters(**filters)), column, CLASS_ORDER[column])


def test_abc_summary(db):
    # rows: A = SKU 1 at both branches (Тасарсан, ROP-оос доош), B = SKU 2 (Хэвийн, Илүүдэл),
    # C = SKU 3 at B001 (no ROP), unknown = SKU 3 at B002 ('#' in the workbook)
    cells, classes = _classes(db, "ABC")
    assert classes["Class"].tolist() == [TOTAL, "A", "B", "C", UNKNOWN_CLASS]
    c = classes.set_index("Class")
    assert c["Rows"].tolist() == [6, 2, 2, 1, 1]
    assert c["RiskRows"].tolist() == [2, 2, 0, 0, 0]
    assert c.loc["A", "SalesShare"] == pytest.approx(180 / 280)
    assert c.loc[TOTAL, "SalesShare"] == pytest.approx(1)
    assert c.loc["B", "CoveredShare"] == 1 and c.loc["A", "CoveredShare"] == 0
    assert pd.isna(c.loc["C", "CoveredShare"])  # its only row has no ROP
    assert c.loc[TOTAL, "CoveredShare"] == pytest.approx(2 / 5)  # same as the coverage tab
    a = cells[cells["Class"] == "A"].set_index("Status")["Share"]
    assert a.to_dict() == {"Тасарсан": 0.5, "ROP-оос доош": 0.5}


def test_xyz_summary_and_filters(db):
    cells, classes = _classes(db, "XYZ")
    assert classes.set_index("Class")["Rows"].to_dict() == {TOTAL: 6, "X": 1, "Y": 2, "Z": 2, UNKNOWN_CLASS: 1}
    # both rollups come from one breakdown, so their totals agree
    _, abc = _classes(db, "ABC")
    measures = ["Rows", "RiskRows", "EligibleRows", "CoveredRows", "ROP", "Inventory", "Gap", "Excess", "Sales"]
    assert classes.iloc[0][measures].tolist() == pytest.approx(abc.iloc[0][measures].tolist())
    assert cells.groupby("Class")["Share"].sum().tolist() == pytest.approx([1.0] * 4)
    # the other dimension filters: only the A rows, split by XYZ
    _, only_a = _classes(db, "XYZ", abc=("A",))
    assert only_a.set_index("Class")["Rows"].to_dict() == {TOTAL: 2, "X": 1, "Y": 1}
    # the global filters apply too
    _, risky = _classes(db, "ABC", critical_only=True)
    assert risky["Class"].tolist() == [TOTAL, "A"]
    _, empty = _classes(db, "ABC", abc=("C",), statuses=("Тасарсан",))
    assert empty.empty


def test_class_items_order_and_filters(db):
    items = db.class_items(Filters())
    assert list(zip(items["ABC"], items["XYZ"])) == [
        ("A", "X"), ("A", "Y"), ("B", "Y"), ("B", "Z"), ("C", "Z"), (UNKNOWN_CLASS, UNKNOWN_CLASS)]
    by_xyz = db.class_items(Filters(), "XYZ")
    assert list(zip(by_xyz["XYZ"], by_xyz["ABC"])) == [
        ("X", "A"), ("Y", "A"), ("Y", "B"), ("Z", "B"), ("Z", "C"), (UNKNOWN_CLASS, UNKNOWN_CLASS)]
    with pytest.raises(ValueError):
        db.class_items(Filters(), "VED")
    assert db.class_items(Filters(xyz=("Z",)))["SKU_ID"].tolist() == [2, 3]
    unknown = db.class_items(Filters(abc=(UNKNOWN_CLASS,)))
    assert unknown[["SKU_ID", "BranchName"]].values.tolist() == [[3, "АФ Зүүн э/сан"]]


def test_class_filters_only_touch_their_own_view():
    """ABC / XYZ appear in the WHERE clause only when set, so the other tabs'
    queries (v_status / v_history have no ABC / XYZ columns) are unaffected."""
    assert Filters(veds=("V",)).where() == ("VED IN (?)", ["V"])
    sql, params = Filters(abc=("A",), xyz=("X", "Y")).where()
    assert sql == "ABC IN (?) AND XYZ IN (?, ?)" and params == ["A", "X", "Y"]
