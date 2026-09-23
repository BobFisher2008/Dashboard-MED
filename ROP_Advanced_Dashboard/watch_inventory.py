"""Watch a folder for new inventory exports and refresh the warehouse automatically.

Drop an XLSX / XLSB / CSV inventory export into the watched folder (default:
``inbox/``). For every new file the watcher:

1. waits until the file has finished copying (size stable and not locked),
2. works out the snapshot date from the file name (fallback: file date),
3. runs ``refresh_inventory.py`` and then ``warehouse.py check``,
4. moves the file to ``inbox/processed/`` or ``inbox/failed/`` (with a .log),
5. runs the optional ``--on-success`` / ``--on-failure`` hook commands.

Files already in the folder when the watcher starts are processed first.
Identical content dropped twice is detected by SHA-256 and not re-run.

    python watch_inventory.py                     # watch inbox/ until Ctrl+C
    python watch_inventory.py --once              # process what is there, then exit
    python watch_inventory.py --watch-dir .       # watch the project folder itself
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

BASE_DIR = Path(__file__).resolve().parent
INVENTORY_SUFFIXES = {".xlsx", ".xlsb", ".csv"}
# Project files that must never be treated as inventory drops when the
# project folder itself is watched.
IGNORED_NAMES = {"sku category.csv", "ved ангилал.csv", "rop final.xlsb"}
TEMP_PREFIXES = ("~$", ".")  # Excel lock files, hidden/partial files
STATE_FILE = ".watcher_state.json"

log = logging.getLogger("inventory_watcher")


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_watch_inventory.py)
# --------------------------------------------------------------------------
def is_inventory_file(path: Path) -> bool:
    name = path.name
    return (
        path.suffix.lower() in INVENTORY_SUFFIXES
        and not name.startswith(TEMP_PREFIXES)
        and name.lower() not in IGNORED_NAMES
    )


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if 2000 <= y <= 2099 else None
    except ValueError:
        return None


def snapshot_date_from_name(name: str, today: date | None = None) -> date | None:
    """Best-effort snapshot date from a file name.

    Recognises 2026-09-16 / 2026_09_16 / 2026.09.16, 20260916 (yyyymmdd),
    09162026 (mmddyyyy), 16092026 (ddmmyyyy) and 0916 (mmdd, current year;
    last year if that would be more than a week in the future).
    """
    today = today or date.today()
    stem = Path(name).stem
    m = re.search(r"(?<!\d)(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)", stem)
    if m and (d := _valid(int(m[1]), int(m[2]), int(m[3]))):
        return d
    for run in re.findall(r"(?<!\d)(\d{8})(?!\d)", stem):
        for y, mo, dd in ((run[:4], run[4:6], run[6:]), (run[4:], run[:2], run[2:4]), (run[4:], run[2:4], run[:2])):
            if d := _valid(int(y), int(mo), int(dd)):
                return d
    for run in re.findall(r"(?<!\d)(\d{4})(?!\d)", stem):
        d = _valid(today.year, int(run[:2]), int(run[2:]))
        if d:
            return d if d <= today + timedelta(days=7) else _valid(today.year - 1, d.month, d.day)
    return None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_until_stable(path: Path, settle: float, timeout: float, poll: float = 0.5) -> bool:
    """True once the file size/mtime stop changing for ``settle`` seconds and
    the file can be opened for writing (i.e. the copy / Excel save is done)."""
    deadline = time.monotonic() + timeout
    last = None
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        try:
            st = path.stat()
        except FileNotFoundError:
            return False
        sig = (st.st_size, st.st_mtime_ns)
        if sig != last:
            last, stable_since = sig, time.monotonic()
        elif st.st_size > 0 and time.monotonic() - stable_since >= settle:
            try:
                with path.open("r+b"):
                    return True
            except PermissionError:
                stable_since = time.monotonic()  # still locked by the writer
        time.sleep(poll)
    return False


# --------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------
@dataclass
class WatcherConfig:
    watch_dir: Path
    project: Path = BASE_DIR
    settle: float = 5.0
    stable_timeout: float = 600.0
    refresh_timeout: float = 1800.0
    extra_args: list[str] = field(default_factory=list)
    on_success: str | None = None
    on_failure: str | None = None
    run_check: bool = True


class InventoryWatcher:
    def __init__(self, cfg: WatcherConfig):
        self.cfg = cfg
        self.processed_dir = cfg.watch_dir / "processed"
        self.failed_dir = cfg.watch_dir / "failed"
        for d in (cfg.watch_dir, self.processed_dir, self.failed_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.state_path = cfg.watch_dir / STATE_FILE
        self.state = self._load_state()
        self.jobs: queue.Queue[Path | None] = queue.Queue()
        self._pending: set[Path] = set()
        self._lock = threading.Lock()

    # ---- state -----------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"processed": {}}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- queue -----------------------------------------------------------
    def enqueue(self, path: Path) -> None:
        path = path.resolve()
        if path.parent != self.cfg.watch_dir.resolve() or not is_inventory_file(path):
            return
        with self._lock:
            if path in self._pending:
                return
            self._pending.add(path)
        log.info("Detected %s", path.name)
        self.jobs.put(path)

    def scan_existing(self) -> None:
        for path in sorted(self.cfg.watch_dir.iterdir(), key=lambda p: p.stat().st_mtime):
            if path.is_file():
                self.enqueue(path)

    def worker(self) -> None:
        while True:
            path = self.jobs.get()
            if path is None:
                return
            try:
                self.process(path)
            except Exception:  # never let one bad file kill the watcher
                log.exception("Unexpected error while processing %s", path.name)
            finally:
                with self._lock:
                    self._pending.discard(path)
                self.jobs.task_done()

    # ---- processing ------------------------------------------------------
    def _move(self, path: Path, dest_dir: Path) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"{stamp}_{path.name}"
        shutil.move(str(path), dest)
        return dest

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            args, cwd=self.cfg.project, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )

    def _hook(self, command: str | None, env_extra: dict[str, str]) -> None:
        if not command:
            return
        try:
            subprocess.run(command, shell=True, cwd=self.cfg.project, timeout=300,
                           env={**os.environ, **env_extra})
        except Exception:
            log.exception("Hook failed: %s", command)

    def process(self, path: Path) -> str:
        if not wait_until_stable(path, self.cfg.settle, self.cfg.stable_timeout):
            if path.exists():
                log.error("%s never finished copying within %ss; left in place", path.name, self.cfg.stable_timeout)
            return "unstable"

        digest = file_sha256(path)
        previous = self.state["processed"].get(digest)
        if previous and previous.get("status") == "ok":
            dest = self._move(path, self.processed_dir)
            log.info("%s has the same content as %s (processed %s); skipped -> %s",
                     path.name, previous["file"], previous["at"], dest.name)
            return "duplicate"

        snap = snapshot_date_from_name(path.name)
        source = "file name"
        if snap is None:
            snap = datetime.fromtimestamp(path.stat().st_mtime).date()
            source = "file modified date"
        log.info("Refreshing from %s (snapshot date %s, from %s)", path.name, snap.isoformat(), source)

        started = time.monotonic()
        refresh = [sys.executable, str(self.cfg.project / "refresh_inventory.py"),
                   "--inventory", str(path), "--snapshot-date", snap.isoformat(),
                   "--project", str(self.cfg.project), *self.cfg.extra_args]
        output: list[str] = []
        status = "failed"
        try:
            result = self._run(refresh, self.cfg.refresh_timeout)
            output += [f"$ {' '.join(refresh)}", result.stdout, result.stderr]
            if result.returncode == 0:
                status = "ok"
                if self.cfg.run_check:
                    check = self._run([sys.executable, str(self.cfg.project / "warehouse.py"), "check"], 600)
                    output += ["$ warehouse.py check", check.stdout, check.stderr]
                    if check.returncode != 0:
                        status = "check_failed"
            else:
                log.error("refresh_inventory.py exited with %s", result.returncode)
        except subprocess.TimeoutExpired:
            output.append(f"Timed out after {self.cfg.refresh_timeout}s")
            log.error("Refresh timed out after %ss", self.cfg.refresh_timeout)

        elapsed = time.monotonic() - started
        dest_dir = self.processed_dir if status == "ok" else self.failed_dir
        dest = self._move(path, dest_dir)
        dest.with_name(dest.name + ".log").write_text("\n".join(o for o in output if o), encoding="utf-8")

        self.state["processed"][digest] = {
            "file": path.name, "moved_to": str(dest.relative_to(self.cfg.watch_dir)),
            "snapshot_date": snap.isoformat(), "date_source": source,
            "status": status, "seconds": round(elapsed, 1), "at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_state()

        level = logging.INFO if status == "ok" else logging.ERROR
        log.log(level, "%s: %s in %.1fs -> %s", status.upper(), path.name, elapsed, dest.relative_to(self.cfg.watch_dir))
        hook_env = {"INVENTORY_FILE": str(dest), "SNAPSHOT_DATE": snap.isoformat(), "REFRESH_STATUS": status}
        self._hook(self.cfg.on_success if status == "ok" else self.cfg.on_failure, hook_env)
        return status


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: InventoryWatcher):
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher.enqueue(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Browsers and Excel write to a temp name, then rename to the final name.
        if not event.is_directory:
            self.watcher.enqueue(Path(event.dest_path))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _single_instance_lock(path: Path):
    """Hold an OS-level lock for the process lifetime; None if already held."""
    fh = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def setup_logging(log_dir: Path, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(log_dir / "inventory_watcher.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stdout is not None:  # pythonw has no console
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-refresh the ROP warehouse when an inventory file is added.")
    parser.add_argument("--watch-dir", type=Path, default=BASE_DIR / "inbox", help="Folder to watch (default: inbox/)")
    parser.add_argument("--project", type=Path, default=BASE_DIR)
    parser.add_argument("--settle", type=float, default=5.0, help="Seconds a file must stay unchanged before processing")
    parser.add_argument("--stable-timeout", type=float, default=600.0)
    parser.add_argument("--refresh-timeout", type=float, default=1800.0)
    parser.add_argument("--excess-threshold", type=float, default=None)
    parser.add_argument("--missing-as-zero", action="store_true")
    parser.add_argument("--powerbi", action="store_true")
    parser.add_argument("--no-check", action="store_true", help="Skip `warehouse.py check` after each refresh")
    parser.add_argument("--on-success", help="Shell command run after a successful refresh (env: INVENTORY_FILE, SNAPSHOT_DATE, REFRESH_STATUS)")
    parser.add_argument("--on-failure", help="Shell command run after a failed refresh (same env)")
    parser.add_argument("--once", action="store_true", help="Process files already present, then exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    watch_dir = args.watch_dir.resolve()
    project = args.project.resolve()
    setup_logging(project / "logs", args.verbose)

    lock = _single_instance_lock(project / "logs" / "inventory_watcher.lock")
    if lock is None:
        log.error("Another inventory watcher is already running for %s; exiting.", project)
        raise SystemExit(1)

    extra: list[str] = []
    if args.excess_threshold is not None:
        extra += ["--excess-threshold", str(args.excess_threshold)]
    if args.missing_as_zero:
        extra.append("--missing-as-zero")
    if args.powerbi:
        extra.append("--powerbi")

    watcher = InventoryWatcher(WatcherConfig(
        watch_dir=watch_dir, project=project, settle=args.settle, stable_timeout=args.stable_timeout,
        refresh_timeout=args.refresh_timeout, extra_args=extra,
        on_success=args.on_success, on_failure=args.on_failure, run_check=not args.no_check,
    ))
    worker = threading.Thread(target=watcher.worker, name="refresh-worker", daemon=True)
    worker.start()

    observer = None
    if not args.once:
        observer = Observer()
        observer.schedule(_Handler(watcher), str(watch_dir), recursive=False)
        observer.start()
        log.info("Watching %s for %s files (Ctrl+C to stop)", watch_dir, ", ".join(sorted(INVENTORY_SUFFIXES)))
    watcher.scan_existing()

    try:
        if args.once:
            watcher.jobs.join()
        else:
            while observer.is_alive():
                observer.join(1)
    except KeyboardInterrupt:
        log.info("Stopping watcher")
    finally:
        if observer:
            observer.stop()
            observer.join()
        watcher.jobs.put(None)
        worker.join(timeout=5)
        lock.close()


if __name__ == "__main__":
    main()
