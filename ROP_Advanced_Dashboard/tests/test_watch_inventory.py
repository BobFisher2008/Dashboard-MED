import json
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from watch_inventory import (
    InventoryWatcher,
    WatcherConfig,
    is_inventory_file,
    snapshot_date_from_name,
    wait_until_stable,
)

TODAY = date(2026, 9, 18)


@pytest.mark.parametrize("name, expected", [
    ("Үлд 20260813 агуулахгүй.xlsx", date(2026, 8, 13)),   # yyyymmdd
    ("ҮЛД0915Эцсийн.xlsx", date(2026, 9, 15)),              # mmdd, current year
    ("inventory_09072026.csv", date(2026, 9, 7)),           # mmddyyyy
    ("inventory_16092026.csv", date(2026, 9, 16)),          # ddmmyyyy
    ("stock 2026-09-18.xlsb", date(2026, 9, 18)),
    ("stock_2026_9_1.csv", date(2026, 9, 1)),
    ("ҮЛД1231.xlsx", date(2025, 12, 31)),                   # would be future -> last year
    ("Үлдэгдэл эцсийн.xlsx", None),
    ("export 123456789.csv", None),                         # 9 digits: not a date
])
def test_snapshot_date_from_name(name, expected):
    assert snapshot_date_from_name(name, today=TODAY) == expected


@pytest.mark.parametrize("name, expected", [
    ("ҮЛД0915Эцсийн.xlsx", True),
    ("export.CSV", True),
    ("rop.xlsb", True),
    ("~$ҮЛД0915.xlsx", False),       # Excel lock file
    (".partial.csv", False),
    ("notes.txt", False),
    ("download.crdownload", False),
    ("SKU category.csv", False),     # project reference file
    ("VED ангилал.csv", False),
    ("ROP final.xlsb", False),
])
def test_is_inventory_file(name, expected):
    assert is_inventory_file(Path(name)) is expected


def test_wait_until_stable_waits_for_writer(tmp_path):
    path = tmp_path / "growing.csv"
    path.write_text("a\n")

    def keep_writing():
        for _ in range(4):
            time.sleep(0.3)
            with path.open("a") as fh:
                fh.write("more\n")

    writer = threading.Thread(target=keep_writing)
    started = time.monotonic()
    writer.start()
    assert wait_until_stable(path, settle=0.6, timeout=10, poll=0.1)
    writer.join()
    assert time.monotonic() - started >= 1.2 + 0.6 - 0.2  # waited for all writes + settle


def test_wait_until_stable_gives_up_on_missing_file(tmp_path):
    assert not wait_until_stable(tmp_path / "gone.csv", settle=0.1, timeout=1)


# --- end to end with a stub project -------------------------------------
STUB_REFRESH = """
import argparse, json, sys
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--inventory"); p.add_argument("--snapshot-date"); p.add_argument("--project")
a, _ = p.parse_known_args()
if "bad" in Path(a.inventory).name:
    print("cannot parse", file=sys.stderr); sys.exit(2)
calls = Path(a.project) / "calls.jsonl"
with calls.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"file": Path(a.inventory).name, "date": a.snapshot_date}, ensure_ascii=False) + "\\n")
print("refreshed")
"""
STUB_CHECK = "import sys; print('checks ok'); sys.exit(0)\n"


@pytest.fixture
def stub_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "refresh_inventory.py").write_text(STUB_REFRESH, encoding="utf-8")
    (project / "warehouse.py").write_text(STUB_CHECK, encoding="utf-8")
    return project


def _calls(project: Path) -> list[dict]:
    path = project / "calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_process_success_duplicate_and_failure(stub_project, tmp_path):
    inbox = tmp_path / "inbox"
    marker = tmp_path / "hook.txt"
    hook = f'"{sys.executable}" -c "import os; open(r\'{marker}\', \'a\').write(os.environ[\'REFRESH_STATUS\'] + chr(10))"'
    watcher = InventoryWatcher(WatcherConfig(watch_dir=inbox, project=stub_project, settle=0.2,
                                             on_success=hook, on_failure=hook))

    good = inbox / "ҮЛД0918Эцсийн.xlsx"
    good.write_bytes(b"inventory v1")
    assert watcher.process(good) == "ok"
    assert _calls(stub_project) == [{"file": good.name, "date": f"{date.today().year}-09-18"}]
    moved = list((inbox / "processed").glob("*_ҮЛД0918Эцсийн.xlsx"))
    assert len(moved) == 1 and moved[0].with_name(moved[0].name + ".log").read_text(encoding="utf-8").count("refreshed") == 1

    # same bytes under another name: detected, not refreshed again
    again = inbox / "copy of inventory.csv"
    again.write_bytes(b"inventory v1")
    assert watcher.process(again) == "duplicate"
    assert len(_calls(stub_project)) == 1

    bad = inbox / "bad_20260918.csv"
    bad.write_bytes(b"garbage")
    assert watcher.process(bad) == "failed"
    failed_log = next((inbox / "failed").glob("*_bad_20260918.csv.log"))
    assert "cannot parse" in failed_log.read_text(encoding="utf-8")

    state = json.loads((inbox / ".watcher_state.json").read_text(encoding="utf-8"))
    assert sorted(v["status"] for v in state["processed"].values()) == ["failed", "ok"]
    assert marker.read_text().split() == ["ok", "failed"]


def test_watcher_cli_detects_new_file(stub_project, tmp_path):
    """Run the real CLI with watchdog, drop a file, and see it processed."""
    inbox = tmp_path / "inbox"
    script = Path(__file__).resolve().parents[1] / "watch_inventory.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--watch-dir", str(inbox), "--project", str(stub_project), "--settle", "0.5"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while not inbox.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1.0)  # let the observer start
        (inbox / "Үлд 20260919.csv").write_text("SKU,qty\n1,2\n", encoding="utf-8")
        while not _calls(stub_project) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert _calls(stub_project) == [{"file": "Үлд 20260919.csv", "date": "2026-09-19"}]
        log = (stub_project / "logs" / "inventory_watcher.log").read_text(encoding="utf-8")
        assert "Detected Үлд 20260919.csv" in log
    finally:
        proc.terminate()
        proc.wait(timeout=10)
