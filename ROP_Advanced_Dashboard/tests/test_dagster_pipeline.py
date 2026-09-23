"""Tests for the Dagster orchestration layer.

These cover the wiring, not the ETL itself: the asset graph's shape, the
inbox sensor's settle / de-duplication rules, how the check results are
classified, and the inventory asset's file handling. The ETL maths is already
covered by test_metrics.py and test_star_schema.py.
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dagster import AssetKey, DagsterInstance, build_sensor_context, materialize  # noqa: E402

from dagster_rop.assets import batch_expiry, inventory_snapshot  # noqa: E402
from dagster_rop.checks import (  # noqa: E402
    _is_measure,
    _is_row_count,
    expiry_completeness_check,
    expiry_data_quality_check,
    mapping_rate_check,
    snapshot_freshness_check,
)
from dagster_rop.definitions import defs  # noqa: E402
from dagster_rop.resources import DuckDBWarehouse, RopProject  # noqa: E402
from dagster_rop.sensors import inbox_sensor  # noqa: E402


# --------------------------------------------------------------------------
# Graph shape
# --------------------------------------------------------------------------
def test_asset_graph_matches_the_etl():
    graph = defs.resolve_asset_graph()
    keys = {k.to_user_string() for k in graph.get_all_asset_keys()}
    assert keys == {
        "rop_workbook", "sku_category_csv", "ved_classification_csv", "inventory_export",
        "branch_rop", "inventory_snapshot", "batch_expiry", "status_history", "warehouse_db",
    }

    def parents(name: str) -> set[str]:
        return {k.to_user_string() for k in graph.get(AssetKey(name)).parent_keys}

    assert parents("branch_rop") == {"rop_workbook"}
    assert parents("inventory_snapshot") == {"branch_rop", "inventory_export"}
    assert parents("batch_expiry") == {"inventory_snapshot", "branch_rop"}
    assert parents("status_history") == {"inventory_snapshot"}
    assert parents("warehouse_db") == {"status_history", "batch_expiry", "sku_category_csv", "ved_classification_csv"}


def test_every_check_hangs_off_the_unified_database():
    graph = defs.resolve_asset_graph()
    checks = {c.to_user_string() for c in graph.asset_check_keys}
    assert checks == {
        "warehouse_db:referential_integrity",
        "warehouse_db:measure_ranges",
        "warehouse_db:row_counts",
        "warehouse_db:mapping_rate",
        "warehouse_db:snapshot_freshness",
        "warehouse_db:expiry_completeness",
        "warehouse_db:expiry_data_quality",
    }


def test_jobs_sensors_and_schedules_are_registered():
    assert {j.name for j in defs.jobs} == {
        "inventory_refresh_job", "rop_reload_job", "classification_reload_job", "full_rebuild_job", "warehouse_check_job"
    }
    assert [s.name for s in defs.sensors] == ["inbox_sensor", "source_files_sensor"]
    assert [s.name for s in defs.schedules] == ["daily_warehouse_check"]


def test_refresh_job_skips_the_rop_workbook():
    """A new inventory export must not re-read ROP final.xlsb (26 s of work)."""
    job = next(j for j in defs.jobs if j.name == "inventory_refresh_job")
    selected = {k.to_user_string() for k in job.selection.resolve(defs.resolve_asset_graph())}
    assert selected == {"inventory_snapshot", "batch_expiry", "status_history", "warehouse_db"}
    assert "branch_rop" not in selected


def test_rop_reload_reprojects_the_batches():
    """New demand in the ROP workbook changes every sell-through projection."""
    job = next(j for j in defs.jobs if j.name == "rop_reload_job")
    selected = {k.to_user_string() for k in job.selection.resolve(defs.resolve_asset_graph())}
    assert "batch_expiry" in selected and "inventory_snapshot" not in selected


# --------------------------------------------------------------------------
# Check classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize("key", [
    "negative_rop_rows",
    "fact_sku_status_excluded_branch_rows",
    "fact_status_history_negative_gap",
    "fact_sku_status_invalid_coverage",
])
def test_violation_counters_are_measures_not_row_counts(key):
    """These end in _rows or look like counts but must be zero, so they
    belong to measure_ranges - not to the 'is the table empty' check."""
    assert _is_measure(key)
    assert not _is_row_count(key)


@pytest.mark.parametrize("key", [
    "dim_sku_rows", "dim_branch_rows", "fact_branch_rop_rows",
    "fact_sku_status_rows", "fact_status_history_rows",
])
def test_table_sizes_are_row_counts(key):
    assert _is_row_count(key)
    assert not _is_measure(key)


# --------------------------------------------------------------------------
# Inbox sensor
# --------------------------------------------------------------------------
@pytest.fixture
def stub_project(tmp_path: Path) -> RopProject:
    project = RopProject(project_dir=str(tmp_path))
    project.ensure_inbox()
    (tmp_path / "data").mkdir(exist_ok=True)
    return project


def _tick(project: RopProject, cursor: str | None, instance: DagsterInstance):
    context = build_sensor_context(cursor=cursor, instance=instance, resources={"project": project})
    return inbox_sensor(context)


def test_sensor_waits_one_tick_before_launching(stub_project):
    """The watcher blocked until the file stopped growing; the sensor instead
    requires the size and mtime to be unchanged between two ticks."""
    export = stub_project.inbox / "Үлд 20260921.csv"
    export.write_text("Салбар,Бараа материал,Эцсийн үлдэгдэл\nА,X,1\n", encoding="utf-8-sig")

    with DagsterInstance.ephemeral() as instance:
        first = _tick(stub_project, None, instance)
        assert not getattr(first, "run_requests", None)  # SkipReason: still settling

        second = _tick(stub_project, first.cursor, instance)

    assert len(second.run_requests) == 1
    config = second.run_requests[0].run_config["ops"]["inventory_snapshot"]["config"]
    assert Path(config["inventory_file"]).name == export.name
    assert config["archive"] is True


def test_sensor_records_the_cursor_even_when_it_skips(stub_project):
    """A skip must still write the cursor. When it did not, the next tick saw
    the file as new again and it stayed "still settling" for ever."""
    export = stub_project.inbox / "Үлд 20260921.csv"
    export.write_text("Салбар,Бараа материал,Эцсийн үлдэгдэл\nА,X,1\n", encoding="utf-8-sig")

    with DagsterInstance.ephemeral() as instance:
        first = _tick(stub_project, None, instance)
        assert not first.run_requests
        assert first.cursor, "a skipping tick must still persist the cursor"
        second = _tick(stub_project, first.cursor, instance)

    assert len(second.run_requests) == 1


def test_sensor_run_key_is_the_content_hash(stub_project):
    """Identical content gets the same run_key, so Dagster refuses the second
    run - which is what inbox/.watcher_state.json used to do."""
    body = "Салбар,Бараа материал,Эцсийн үлдэгдэл\nА,X,1\n"
    a = stub_project.inbox / "Үлд 20260921.csv"
    b = stub_project.inbox / "Үлд 20260921 (copy).csv"
    a.write_text(body, encoding="utf-8-sig")
    b.write_text(body, encoding="utf-8-sig")

    cursor = json.dumps({p.name: [p.stat().st_size, p.stat().st_mtime_ns] for p in (a, b)})
    with DagsterInstance.ephemeral() as instance:
        result = _tick(stub_project, cursor, instance)

    keys = {r.run_key for r in result.run_requests}
    assert len(result.run_requests) == 2
    assert len(keys) == 1, "same content must collapse to one run_key"


def test_sensor_ignores_lock_files_and_reference_data(stub_project):
    for name in ("~$Үлд 20260921.xlsx", ".partial.csv", "SKU category.csv", "VED ангилал.csv", "ROP final.xlsb", "notes.txt"):
        (stub_project.inbox / name).write_text("x", encoding="utf-8")

    with DagsterInstance.ephemeral() as instance:
        result = _tick(stub_project, None, instance)
        assert not getattr(result, "run_requests", None)
        # Nothing was even recorded as settling, so a later tick stays quiet.
        again = _tick(stub_project, "{}", instance)
    assert not getattr(again, "run_requests", None)


def test_sensor_caps_runs_per_tick(stub_project):
    files = []
    for i in range(5):
        p = stub_project.inbox / f"Үлд 2026092{i}.csv"
        p.write_text(f"Салбар,Бараа материал,Эцсийн үлдэгдэл\nА,X,{i}\n", encoding="utf-8-sig")
        files.append(p)
    cursor = json.dumps({p.name: [p.stat().st_size, p.stat().st_mtime_ns] for p in files})

    with DagsterInstance.ephemeral() as instance:
        result = _tick(stub_project, cursor, instance)
    assert len(result.run_requests) == 3  # MAX_RUNS_PER_TICK


# --------------------------------------------------------------------------
# inventory_snapshot asset
# --------------------------------------------------------------------------
def _materialize_inventory(project: RopProject, config: dict, monkeypatch, refresh):
    import dagster_rop.assets as assets_module

    monkeypatch.setattr(assets_module.refresh_inventory, "refresh", refresh)
    return materialize(
        [inventory_snapshot],
        resources={"project": project},
        run_config={"ops": {"inventory_snapshot": {"config": config}}},
    )


_SUMMARY = {
    "snapshot_date": "2026-09-21", "source_file": "x.csv", "scope": "BRANCH",
    "inventory_rows": 3, "status_rows": 3, "history_rows": 6, "matched_rows": 3,
    "unmatched_rows": 0, "mapping_rate": 1.0, "rop_gap": 0.0, "on_hand_total": 9.0,
    "critical_ve_sku": 0, "status_counts": {"Хэвийн": 3},
}


def test_snapshot_date_comes_from_the_file_name(stub_project, monkeypatch):
    export = stub_project.inbox / "Үлд 20260921 агуулахгүй.csv"
    export.write_text("x", encoding="utf-8")
    seen = {}

    def fake_refresh(**kwargs):
        seen.update(kwargs)
        return dict(_SUMMARY)

    result = _materialize_inventory(stub_project, {"inventory_file": str(export)}, monkeypatch, fake_refresh)
    assert result.success
    assert seen["snapshot_date"] == "2026-09-21"


def test_explicit_snapshot_date_wins(stub_project, monkeypatch):
    export = stub_project.inbox / "Үлд 20260921 агуулахгүй.csv"
    export.write_text("x", encoding="utf-8")
    seen = {}

    def fake_refresh(**kwargs):
        seen.update(kwargs)
        return dict(_SUMMARY)

    _materialize_inventory(
        stub_project,
        {"inventory_file": str(export), "snapshot_date": "2026-01-05"},
        monkeypatch, fake_refresh,
    )
    assert seen["snapshot_date"] == "2026-01-05"


def test_successful_run_moves_the_file_to_processed(stub_project, monkeypatch):
    export = stub_project.inbox / "Үлд 20260921.csv"
    export.write_text("x", encoding="utf-8")

    result = _materialize_inventory(stub_project, {"inventory_file": str(export)},
                                    monkeypatch, lambda **kw: dict(_SUMMARY))
    assert result.success
    assert not export.exists()
    assert [p.name for p in stub_project.processed_dir.iterdir()][0].endswith(export.name)
    assert not list(stub_project.failed_dir.iterdir())


def test_failed_run_moves_the_file_to_failed(stub_project, monkeypatch):
    export = stub_project.inbox / "Үлд 20260921.csv"
    export.write_text("x", encoding="utf-8")

    def boom(**kwargs):
        raise ValueError("Үлдэгдлийн багануудыг таньж чадсангүй.")

    with pytest.raises(Exception):
        _materialize_inventory(stub_project, {"inventory_file": str(export)}, monkeypatch, boom)

    assert not export.exists()
    assert [p.name for p in stub_project.failed_dir.iterdir()][0].endswith(export.name)


def test_archive_false_leaves_the_file_in_place(stub_project, monkeypatch):
    export = stub_project.inbox / "Үлд 20260921.csv"
    export.write_text("x", encoding="utf-8")

    _materialize_inventory(stub_project, {"inventory_file": str(export), "archive": False},
                           monkeypatch, lambda **kw: dict(_SUMMARY))
    assert export.exists()


def test_empty_inbox_is_a_clear_error(stub_project, monkeypatch):
    with pytest.raises(Exception, match="inbox|No inventory export"):
        _materialize_inventory(stub_project, {}, monkeypatch, lambda **kw: dict(_SUMMARY))


def test_oldest_inbox_file_is_taken_when_none_configured(stub_project, monkeypatch):
    import os
    import time

    old = stub_project.inbox / "Үлд 20260901.csv"
    new = stub_project.inbox / "Үлд 20260921.csv"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    past = time.time() - 3600
    os.utime(old, (past, past))
    seen = {}

    def fake_refresh(**kwargs):
        seen.update(kwargs)
        return dict(_SUMMARY)

    _materialize_inventory(stub_project, {}, monkeypatch, fake_refresh)
    assert Path(seen["inventory"]).name == old.name


# --------------------------------------------------------------------------
# Metadata-driven checks
# --------------------------------------------------------------------------
def _write_metadata(project: RopProject, payload: dict) -> None:
    project.data_dir.mkdir(parents=True, exist_ok=True)
    (project.data_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")


def test_mapping_rate_check_reads_a_fraction_or_a_percentage(stub_project):
    _write_metadata(stub_project, {"mapping": {"mapping_rate_rows": 0.97, "matched_rows": 97, "unmatched_rows": 3}})
    assert mapping_rate_check(stub_project).passed

    _write_metadata(stub_project, {"mapping": {"mapping_rate_rows": 97.0, "matched_rows": 97, "unmatched_rows": 3}})
    result = mapping_rate_check(stub_project)
    assert result.passed
    assert result.metadata["mapping_rate"].value == pytest.approx(0.97)


def test_mapping_rate_check_warns_below_the_threshold(stub_project):
    _write_metadata(stub_project, {"mapping": {"mapping_rate_rows": 0.5, "matched_rows": 50, "unmatched_rows": 50}})
    assert not mapping_rate_check(stub_project).passed


def test_freshness_check_uses_the_newest_snapshot(stub_project):
    today = date.today()
    _write_metadata(stub_project, {"history_dates": ["2020-01-01", today.isoformat()]})
    result = snapshot_freshness_check(stub_project)
    assert result.passed
    assert result.metadata["age_days"].value == 0

    _write_metadata(stub_project, {"history_dates": ["2020-01-01"]})
    assert not snapshot_freshness_check(stub_project).passed


def test_checks_do_not_crash_without_metadata(stub_project):
    assert not mapping_rate_check(stub_project).passed
    assert not snapshot_freshness_check(stub_project).passed
    assert not expiry_completeness_check(stub_project).passed
    assert not expiry_data_quality_check(stub_project).passed


_EXPIRY = {
    "snapshot_date": "2026-09-21", "expiry_completeness": 0.95, "status_batches": {"UNKNOWN": 5},
    "negative_qty_batches": 0, "implausible_expiry_lines": 0,
}


def test_expiry_completeness_check_warns_below_90_percent(stub_project):
    _write_metadata(stub_project, {"expiry": _EXPIRY})
    assert expiry_completeness_check(stub_project).passed
    _write_metadata(stub_project, {"expiry": {**_EXPIRY, "expiry_completeness": 0.8}})
    result = expiry_completeness_check(stub_project)
    assert not result.passed
    assert result.metadata["unknown_batches"].value == 5


def test_expiry_data_quality_check_flags_negative_stock_and_odd_dates(stub_project):
    _write_metadata(stub_project, {"expiry": _EXPIRY})
    assert expiry_data_quality_check(stub_project).passed
    _write_metadata(stub_project, {"expiry": {**_EXPIRY, "negative_qty_batches": 3}})
    assert not expiry_data_quality_check(stub_project).passed
    _write_metadata(stub_project, {"expiry": {**_EXPIRY, "implausible_expiry_lines": 1}})
    assert not expiry_data_quality_check(stub_project).passed


# --------------------------------------------------------------------------
# Batch expiry asset
# --------------------------------------------------------------------------
def test_batch_expiry_asset_leaves_the_database_to_warehouse_db(stub_project, monkeypatch):
    import dagster_rop.assets as assets_module

    seen = {}

    def fake_refresh(data_dir, config, refresh_db):
        seen.update(data_dir=data_dir, config=config, refresh_db=refresh_db)
        return {
            **_EXPIRY, "source_file": "x.csv", "has_expiry_dates": True, "batch_rows": 10,
            "status_qty": {"EXPIRED": 2.0}, "pull_batches": 1, "at_risk_batches": 3,
            "projected_waste_qty": 7.5, "usable_without_demand_data_batches": 0,
            "config_version": "abc123",
        }

    monkeypatch.setattr(assets_module.expiry, "refresh", fake_refresh)
    result = materialize([batch_expiry], resources={"project": stub_project})
    assert result.success
    assert seen == {"data_dir": stub_project.data_dir, "config": stub_project.expiry_config_path, "refresh_db": False}
    meta = result.asset_materializations_for_node("batch_expiry")[0].metadata
    assert meta["at_risk_batches"].value == 3
    assert meta["config_version"].value == "abc123"


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------
def test_unified_db_resource_lists_tables(tmp_path):
    import duckdb

    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE dim_sku AS SELECT * FROM range(3) t(SKU_ID)")
    con.execute("CREATE TABLE fact_sku_status AS SELECT * FROM range(7) t(SKU_ID)")
    con.close()

    db = DuckDBWarehouse(database=str(path))
    assert db.table_rows() == {"dim_sku": 3, "fact_sku_status": 7}
    assert db.query_one("SELECT count(*) FROM dim_sku") == 3


def test_project_resource_paths(tmp_path):
    project = RopProject(project_dir=str(tmp_path))
    assert project.data_dir == tmp_path / "data"
    assert project.rop_path == tmp_path / "ROP final.xlsb"
    assert project.category_path == tmp_path / "SKU category.csv"
    assert project.ved_path == tmp_path / "VED ангилал.csv"
    assert project.expiry_config_path == tmp_path / "expiry_config.json"
    assert project.inbox == tmp_path / "inbox"
    project.ensure_inbox()
    assert project.processed_dir.is_dir() and project.failed_dir.is_dir()


# --------------------------------------------------------------------------
# source_files_sensor: the ROP workbook and the business-owned lists
# --------------------------------------------------------------------------
def _source_tick(project: RopProject, cursor: str | None, instance: DagsterInstance):
    from dagster_rop.sensors import source_files_sensor
    context = build_sensor_context(cursor=cursor, instance=instance, resources={"project": project})
    return source_files_sensor(context)


def _touch(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a later mtime, as a real save gives


def test_source_sensor_starts_from_a_baseline(stub_project):
    """Starting Dagster must not reload: the first tick only records the files."""
    (stub_project.root / "ROP final.xlsb").write_text("v1", encoding="utf-8")
    with DagsterInstance.ephemeral() as instance:
        first = _source_tick(stub_project, None, instance)
        assert not first.run_requests
        again = _source_tick(stub_project, first.cursor, instance)
        assert not again.run_requests and "No source file changed" in again.skip_reason.skip_message


def test_source_sensor_launches_the_matching_job_once_the_save_settles(stub_project):
    rop, ved = stub_project.root / "ROP final.xlsb", stub_project.root / "VED ангилал.csv"
    rop.write_text("v1", encoding="utf-8")
    ved.write_text("v1", encoding="utf-8")
    with DagsterInstance.ephemeral() as instance:
        cursor = _source_tick(stub_project, None, instance).cursor
        _touch(ved, "v2")
        settling = _source_tick(stub_project, cursor, instance)
        assert not settling.run_requests and "VED" in settling.skip_reason.skip_message
        launched = _source_tick(stub_project, settling.cursor, instance)
        assert [r.job_name for r in launched.run_requests] == ["classification_reload_job"]
        assert not _source_tick(stub_project, launched.cursor, instance).run_requests  # loaded once

        # the workbook and a list together: the ROP reload also rebuilds warehouse_db
        _touch(rop, "v2")
        _touch(ved, "v3")
        settling = _source_tick(stub_project, launched.cursor, instance)
        both = _source_tick(stub_project, settling.cursor, instance)
        assert [r.job_name for r in both.run_requests] == ["rop_reload_job"]
