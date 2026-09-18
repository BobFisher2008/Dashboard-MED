"""Referential integrity of the built warehouse in data/ (skipped if not built)."""
import pytest

from warehouse import DATA_DIR, connect, integrity_checks

pytestmark = pytest.mark.skipif(not (DATA_DIR / "fact_sku_status.parquet").exists(), reason="warehouse not built")


@pytest.fixture(scope="module")
def con():
    con = connect()
    yield con
    con.close()


def test_integrity_checks_pass(con):
    checks = integrity_checks(con)
    assert checks["all_ok"], checks["failed"]


@pytest.mark.parametrize("fact", ["fact_sku_status", "fact_status_history", "fact_branch_rop"])
@pytest.mark.parametrize("key, dim", [("SKU_ID", "dim_sku"), ("Branch_ID", "dim_branch")])
def test_no_orphan_keys(con, fact, key, dim):
    orphans = con.execute(f"SELECT count(*) FROM {fact} ANTI JOIN {dim} USING ({key})").fetchone()[0]
    assert orphans == 0


def test_facts_carry_no_dimension_attributes(con):
    for fact in ["fact_sku_status", "fact_status_history", "fact_branch_rop"]:
        cols = {r[0] for r in con.execute(f"DESCRIBE {fact}").fetchall()}
        assert not {"SKU_Name", "Category", "BranchName", "MasterKey"} & cols, fact


def test_every_history_date_is_in_dim_date(con):
    missing = con.execute(
        "SELECT count(*) FROM (SELECT DISTINCT DateKey FROM fact_status_history) h "
        "ANTI JOIN (SELECT DateKey FROM dim_date WHERE HasSnapshot) d USING (DateKey)"
    ).fetchone()[0]
    assert missing == 0
