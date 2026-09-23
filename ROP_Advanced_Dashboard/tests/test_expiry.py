"""Batch expiry: reading the expiry column, status, FEFO and the sell-through projection."""
import json

import numpy as np
import pandas as pd
import pytest

from common import build_status_snapshot, parse_expiry_dates, parse_inventory
from expiry import (
    EXPIRY_STATUS_ORDER,
    ExpiryConfig,
    build_batch_expiry,
    build_dim_expiry_status,
    project_sell_through,
    refresh,
)
from warehouse import branch_rop_wide, integrity_checks, to_fact_branch_rop, to_fact_status

SNAPSHOT = "2026-09-21"
BRANCH_NAMES = {"B001": "АФ Төв э/сан", "B002": "АФ Зүүн э/сан", "B003": "Мед_Премиум э/сан", "NETWORK": "NETWORK"}


def _inventory(rows) -> pd.DataFrame:
    """rows: (SKU_ID, Branch_ID, expiry date, OnHand)"""
    df = pd.DataFrame(rows, columns=["SKU_ID", "Branch_ID", "ExpiryDate", "OnHand"])
    df["SKU_ID"] = df["SKU_ID"].astype("Int64")
    df["BranchName"] = df["Branch_ID"].map(BRANCH_NAMES)
    df["ExpiryDate"] = pd.to_datetime(df["ExpiryDate"])
    return df


@pytest.fixture
def build(dim_sku):
    """build(rows, demand=[(SKU_ID, Branch_ID, monthly demand)], cfg, history)"""
    def _build(rows, demand=(), cfg=None, history=None):
        rop = pd.DataFrame(list(demand), columns=["SKU_ID", "Branch_ID", "AvgMonthlyDemand_Branch"])
        return build_batch_expiry(_inventory(rows), dim_sku, rop, SNAPSHOT, cfg, history)
    return _build


# --------------------------------------------------------------------------
# Reading the expiry column
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("11/30/2026", "2026-11-30"),            # the ERP export: m/d/yyyy
    ("11/30/2026 0:00:00", "2026-11-30"),
    ("11/2026", "2026-11-30"),               # month only = last day of that month
    ("2/2028", "2028-02-29"),
    ("2026-11-30", "2026-11-30"),
    ("30.11.2026", "2026-11-30"),
    (46356, "2026-11-30"),                   # Excel serial day (xlsb)
    (pd.Timestamp("2026-11-30 13:45"), "2026-11-30"),
    ("", None), (None, None), ("хугацаагүй", None), ("13/13/2026", None),
])
def test_parse_expiry_dates(raw, expected):
    parsed = parse_expiry_dates(pd.Series([raw], dtype=object)).iloc[0]
    if expected is None:
        assert pd.isna(parsed)
    else:
        assert parsed == pd.Timestamp(expected)


def test_slash_dates_read_day_first_only_when_a_value_requires_it():
    ambiguous = parse_expiry_dates(pd.Series(["01/02/2026", "03/04/2026"]))
    assert ambiguous.tolist() == [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-03-04")]
    day_first = parse_expiry_dates(pd.Series(["01/02/2026", "25/04/2026"]))
    assert day_first.tolist() == [pd.Timestamp("2026-02-01"), pd.Timestamp("2026-04-25")]


def test_parse_inventory_keeps_the_expiry_column(tmp_path):
    csv = tmp_path / "Үлд 0921.csv"
    csv.write_text(
        "Агуулах,Бараа материал,Сери.Хүртэл хүчинтэй,Эцсийн үлдэгдэл\n"
        "АФ Төв э/сан,Тобрекс дусал,11/30/2026,2\n"
        "АФ Төв э/сан,Тобрекс дусал,,1\n"
        "АФ Төв э/сан,Тобрекс дусал,хугацаагүй,3\n",
        encoding="utf-8-sig",
    )
    out, meta = parse_inventory(csv)
    assert meta["column_map"]["expiry"] == "Сери.Хүртэл хүчинтэй"
    assert out["ExpiryDate"].iloc[0] == pd.Timestamp("2026-11-30")
    assert out["ExpiryDate"].iloc[1:].isna().all()
    assert out["ExpiryRaw"].tolist() == ["", "", "хугацаагүй"]  # only what could not be read


def test_an_expiry_date_header_is_not_the_snapshot_date(tmp_path):
    """'Дуусах огноо' (expiry date) contains 'огноо', the snapshot-date alias."""
    csv = tmp_path / "export.csv"
    csv.write_text("Салбар,Нэр төрөл,Дуусах огноо,Үлдэгдэл\nАФ Төв э/сан,Тобрекс дусал,2026-11-30,2\n", encoding="utf-8")
    out, meta = parse_inventory(csv)
    assert meta["column_map"]["expiry"] == "Дуусах огноо"
    assert meta["column_map"]["snapshot_date"] is None
    assert out["SnapshotDate"].isna().all()
    assert out["ExpiryDate"].iloc[0] == pd.Timestamp("2026-11-30")


# --------------------------------------------------------------------------
# Status and flags
# --------------------------------------------------------------------------
@pytest.mark.parametrize("expiry, status", [
    ("2026-09-20", "EXPIRED"),
    ("2026-09-21", "CRITICAL"), ("2026-10-21", "CRITICAL"),   # DTE 0, 30
    ("2026-10-22", "WARNING"), ("2026-12-20", "WARNING"),     # DTE 31, 90
    ("2026-12-21", "WATCH"), ("2027-03-20", "WATCH"),         # DTE 91, 180
    ("2027-03-21", "OK"),
    (None, "UNKNOWN"),
    ("2040-01-01", "UNKNOWN"),                                # more than 10 years ahead
    ("1999-12-31", "UNKNOWN"),
])
def test_status_boundaries(build, expiry, status):
    assert build([(1, "B001", expiry, 5)]).iloc[0]["ExpiryStatus"] == status


def test_a_batch_inside_min_dispense_days_is_pulled(build):
    """Spec 5.3: expiry 2026-10-15 on 2026-09-21 -> DTE 24, Critical, PULL."""
    row = build([(1, "B001", "2026-10-15", 5)], demand=[(1, "B001", 100.0)]).iloc[0]
    assert row["DTE"] == 24
    assert row["ExpiryStatus"] == "CRITICAL"
    assert row["PullFlag"] and not row["IsUsable"]
    assert pd.isna(row["FEFORank"])
    assert row["ProjectedWaste"] == 5 and row["AtRisk"]  # cannot be dispensed, whatever the demand


def test_expired_stock_is_all_waste(build):
    row = build([(1, "B001", "2026-09-01", 7)], demand=[(1, "B001", 100.0)]).iloc[0]
    assert row["ExpiryStatus"] == "EXPIRED" and not row["PullFlag"]
    assert row["ProjectedSold"] == 0 and row["ProjectedWaste"] == 7 and row["AtRisk"]


def test_unknown_expiry_is_left_out_of_fefo_and_projection(build):
    b = build([(1, "B001", None, 4), (1, "B001", "2027-06-30", 2)], demand=[(1, "B001", 30.0)])
    unknown = b[b["ExpiryStatus"] == "UNKNOWN"].iloc[0]
    assert pd.isna(unknown["ExpiryDateKey"]) and pd.isna(unknown["DTE"])
    assert pd.isna(unknown["FEFORank"]) and pd.isna(unknown["ProjectedWaste"]) and not unknown["AtRisk"]
    assert b.loc[b["ExpiryStatus"] != "UNKNOWN", "FEFORank"].tolist() == [1]


def test_negative_stock_is_not_usable_or_at_risk(build):
    row = build([(1, "B001", "2026-10-01", -2)], demand=[(1, "B001", 10.0)]).iloc[0]
    assert not row["IsUsable"] and not row["PullFlag"] and not row["AtRisk"]
    assert pd.isna(row["FEFORank"]) and pd.isna(row["ProjectedWaste"])


def test_min_dispense_days_by_category_and_sku(build):
    cfg = ExpiryConfig(min_dispense_days_by_category={"Импорт": 60}, min_dispense_days_by_sku={2: 10})
    b = build([(sku, "B001", "2026-11-05", 5) for sku in (1, 2, 3)], cfg=cfg).set_index("SKU_ID")  # DTE 45
    assert b["MinDispenseDays"].to_dict() == {1: 60, 2: 10, 3: 30}   # SKU 1 is Импорт, SKU 3 the default
    assert b["PullFlag"].to_dict() == {1: True, 2: False, 3: False}


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------
def test_lines_with_the_same_batch_key_are_summed(build):
    b = build([
        (1, "B001", "2026-12-31", 2), (1, "B001", "2026-12-31", 3), (1, "B001", "2026-12-31", -1),
        (None, "B001", "2026-12-31", 9),                       # unmatched SKU: not a batch
        (1, "B003", "2026-12-31", 9),                          # excluded branch
    ])
    assert len(b) == 1
    assert b.iloc[0]["Qty"] == 4 and b.iloc[0]["InventoryLines"] == 3


def test_batches_add_up_to_the_status_on_hand(dim_sku, dim_branch, branch_rop, inventory):
    inv = inventory.assign(ExpiryDate=pd.to_datetime(
        ["2026-12-31", "2027-01-31", "2027-01-31", None, "2026-10-01", "2026-12-31"]))
    rop = branch_rop_wide(to_fact_branch_rop(branch_rop), dim_branch, dim_sku)
    status = to_fact_status(build_status_snapshot(dim_sku, inv, rop, SNAPSHOT))
    batches = build_batch_expiry(inv, dim_sku, to_fact_branch_rop(branch_rop), SNAPSHOT)

    on_hand = status.dropna(subset=["OnHand"]).set_index(["SKU_ID", "Branch_ID"])["OnHand"].to_dict()
    qty = batches.groupby(["SKU_ID", "Branch_ID"])["Qty"].sum().to_dict()
    assert qty == pytest.approx(on_hand)


def test_first_seen_comes_from_the_history(build):
    history = build([(1, "B001", "2026-12-31", 5), (1, "B001", None, 1)]).assign(DateKey=20260907)
    b = build([(1, "B001", "2026-12-31", 4), (1, "B001", "2027-12-31", 9), (1, "B001", None, 1)], history=history)
    # sorted by expiry, unknown last
    assert b["ExpiryDateKey"].tolist()[:2] == [20261231, 20271231]
    assert b["FirstSeenDateKey"].tolist() == [20260907, 20260921, 20260907]


# --------------------------------------------------------------------------
# FEFO and the sell-through projection
# --------------------------------------------------------------------------
def test_earliest_expiry_is_picked_first(build):
    b = build([(1, "B001", "2027-03-31", 5), (1, "B001", "2026-12-31", 5), (1, "B002", "2027-03-31", 5)])
    ranks = b.set_index(["Branch_ID", "ExpiryDateKey"])["FEFORank"]
    assert ranks[("B001", 20261231)] == 1
    assert ranks[("B001", 20270331)] == 2
    assert ranks[("B002", 20270331)] == 1


def test_single_batch_projection(build):
    """Spec 5.3: Qty 100, DTE 60, demand 30/month -> ~30 sold, ~70 wasted."""
    row = build([(1, "B001", "2026-11-20", 100)], demand=[(1, "B001", 30.0)]).iloc[0]
    sold = 30 / 30.4 * (60 - 30)
    assert row["ProjectedSold"] == pytest.approx(sold)
    assert row["ProjectedWaste"] == pytest.approx(100 - sold)
    assert row["AtRisk"]


def test_later_batch_takes_the_demand_the_earlier_one_leaves(build):
    """Spec 5.3: A (Qty 50, DTE 45) and B (Qty 50, DTE 120), demand 60/month:
    A wastes ~20, B nothing."""
    b = build([(1, "B001", "2026-11-05", 50), (1, "B001", "2027-01-19", 50)], demand=[(1, "B001", 60.0)])
    daily = 60 / 30.4
    assert b["ProjectedWaste"].tolist() == pytest.approx([50 - daily * 15, 0.0])

    # A small first batch sells out; B sells the rest of the demand to its sell-by day.
    b = build([(1, "B001", "2026-11-05", 10), (1, "B001", "2027-01-19", 200)], demand=[(1, "B001", 60.0)])
    assert b["ProjectedSold"].tolist() == pytest.approx([10.0, daily * 90 - 10])


def test_no_demand_figure_means_no_projection_but_zero_demand_is_all_waste(build):
    b = build([(1, "B001", "2027-06-30", 5), (1, "B002", "2027-06-30", 5)], demand=[(1, "B002", 0.0)])
    b = b.set_index("Branch_ID")
    not_listed, zero = b.loc["B001"], b.loc["B002"]
    assert not not_listed["HasDemandData"] and not_listed["FEFORank"] == 1
    assert pd.isna(not_listed["ProjectedWaste"]) and not not_listed["AtRisk"]
    assert zero["HasDemandData"] and zero["ProjectedWaste"] == 5 and zero["AtRisk"]


def test_network_rows_use_the_network_demand(build):
    row = build([(1, "NETWORK", "2026-11-20", 100)]).iloc[0]
    assert row["DailyDemand"] == pytest.approx(5 / 30.4)  # dim_sku.AvgMonthlyDemand of SKU 1


def _fefo_loop(qty, dte, min_days, daily):
    """The projection as spec section 2.4 writes it."""
    sold_before, out = 0.0, []
    for q, t in zip(qty, dte):
        capacity = max(0.0, daily * max(0, t - min_days) - sold_before)
        out.append(min(q, capacity))
        sold_before += out[-1]
    return out


def test_vectorised_projection_matches_the_fefo_loop():
    rng = np.random.default_rng(7)
    groups = []
    for sku in range(300):
        n = int(rng.integers(1, 7))
        dte = np.sort(rng.choice(np.arange(30, 500), size=n, replace=False))
        groups.append(pd.DataFrame({
            "SKU_ID": sku, "Branch_ID": "B001", "ExpiryDateKey": dte, "DTE": dte,
            "Qty": rng.uniform(0.5, 120, n).round(1), "MinDispenseDays": 30,
            "DailyDemand": rng.choice([0.0, 0.2, 1.5, 6.0]), "IsUsable": True,
        }))
    batches = pd.concat(groups, ignore_index=True).sample(frac=1, random_state=1)  # order must not matter
    projected = project_sell_through(batches)
    for _, rows in batches.groupby("SKU_ID"):
        rows = rows.sort_values("ExpiryDateKey")
        expected = _fefo_loop(rows["Qty"], rows["DTE"], 30, rows["DailyDemand"].iloc[0])
        assert projected.loc[rows.index, "ProjectedSold"].tolist() == pytest.approx(expected, abs=1e-9)
        assert projected.loc[rows.index, "FEFORank"].tolist() == list(range(1, len(rows) + 1))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def test_config_defaults_and_file(tmp_path):
    assert ExpiryConfig.load(tmp_path / "missing.json") == ExpiryConfig()
    path = tmp_path / "expiry_config.json"
    path.write_text(json.dumps({"_comment": "x", "warning_days": 120, "min_dispense_days_by_sku": {"7": 14}}),
                    encoding="utf-8")
    cfg = ExpiryConfig.load(path)
    assert cfg.warning_days == 120
    assert cfg.min_dispense_days_by_sku == {7: 14}
    assert cfg.version != ExpiryConfig().version


def test_config_rejects_typos_and_bad_ordering(tmp_path):
    path = tmp_path / "expiry_config.json"
    path.write_text(json.dumps({"warning_day": 120}), encoding="utf-8")
    with pytest.raises(ValueError, match="warning_day"):
        ExpiryConfig.load(path)
    with pytest.raises(ValueError):
        ExpiryConfig(critical_days=100, warning_days=90)


def test_the_shipped_config_is_the_defaults():
    assert ExpiryConfig.load() == ExpiryConfig()


def test_status_dimension_follows_the_thresholds():
    dim = build_dim_expiry_status(ExpiryConfig(critical_days=14))
    assert dim["ExpiryStatus"].tolist() == EXPIRY_STATUS_ORDER
    assert dim.set_index("ExpiryStatus").loc["CRITICAL", "StatusLabel"] == "0–14 хоног"
    assert dim.set_index("ExpiryStatus").loc["WARNING", "MinDays"] == 15


# --------------------------------------------------------------------------
# Refresh and integrity
# --------------------------------------------------------------------------
def test_refresh_writes_the_fact_and_replaces_its_date_in_history(tmp_path, dim_sku, branch_rop, inventory):
    data = tmp_path / "data"
    data.mkdir()
    inv = inventory.assign(ExpiryDate=pd.Timestamp("2026-12-31"), SnapshotDate=SNAPSHOT, SourceFile="Үлд 0921.csv")
    dim_sku.to_parquet(data / "dim_sku.parquet")
    to_fact_branch_rop(branch_rop).to_parquet(data / "fact_branch_rop.parquet")
    inv.to_parquet(data / "fact_inventory_snapshot.parquet")
    (data / "metadata.json").write_text(json.dumps({"snapshot_date": SNAPSHOT}), encoding="utf-8")

    summary = refresh(data, config=None, refresh_db=False)
    refresh(data, config=None, refresh_db=False)  # same snapshot again

    fact = pd.read_parquet(data / "fact_batch_expiry.parquet")
    history = pd.read_parquet(data / "fact_batch_expiry_history.parquet")
    assert len(fact) == summary["batch_rows"] == len(history)  # replaced, not appended twice
    assert (data / "dim_expiry_status.parquet").exists()
    meta = json.loads((data / "metadata.json").read_text(encoding="utf-8"))
    assert meta["expiry"]["snapshot_date"] == SNAPSHOT
    assert meta["expiry"]["expiry_completeness"] == 1.0


def test_refresh_needs_an_inventory_snapshot(tmp_path):
    with pytest.raises(FileNotFoundError, match="refresh_inventory"):
        refresh(tmp_path, config=None, refresh_db=False)


def _checks_with(db, batches: pd.DataFrame) -> dict:
    db.con.register("_b", batches)
    db.con.execute("CREATE OR REPLACE TABLE fact_batch_expiry AS SELECT * FROM _b")
    db.con.unregister("_b")
    return integrity_checks(db.con)


def test_integrity_checks_reconcile_batches_with_on_hand(db, dim_sku, branch_rop, inventory):
    """db's fact_sku_status is the conftest inventory on 2026-09-16."""
    inv = inventory.assign(ExpiryDate=pd.to_datetime(
        ["2026-12-31", "2027-01-31", "2027-01-31", None, "2026-10-01", "2026-12-31"]))
    batches = build_batch_expiry(inv, dim_sku, to_fact_branch_rop(branch_rop), "2026-09-16")

    checks = _checks_with(db, batches)
    assert checks["fact_batch_expiry_unique_key"]
    assert checks["fact_batch_expiry_invalid_snapshot"] == 0
    assert checks["fact_batch_expiry_invalid_reconciliation"] == 0
    assert not [k for k in checks["failed"] if k.startswith("fact_batch_expiry")]

    unbalanced = batches.assign(Qty=batches["Qty"] + np.where(batches.index == 0, 1.0, 0.0))
    assert _checks_with(db, unbalanced)["fact_batch_expiry_invalid_reconciliation"] == 1

    stale = batches.assign(DateKey=20260911)
    checks = _checks_with(db, stale)
    assert checks["fact_batch_expiry_invalid_snapshot"] == 1
    assert "fact_batch_expiry_invalid_reconciliation" not in checks
    assert "fact_batch_expiry_invalid_snapshot" in checks["failed"]
