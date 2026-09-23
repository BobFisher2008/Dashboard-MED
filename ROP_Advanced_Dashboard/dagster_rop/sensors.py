"""The inbox sensor - the Dagster replacement for watch_inventory.py.

It keeps the watcher's two hard-won behaviours:

1. **Never read a half-copied file.** The watcher blocked until the size and
   mtime stopped changing. A sensor must not block, so instead it records
   (size, mtime) in its cursor and only launches a run on the *next* tick if
   they are unchanged - one tick interval of quiet stands in for the settle
   window.
2. **Never load the same content twice.** The watcher kept a SHA-256 in
   ``inbox/.watcher_state.json``. Here the digest is the ``run_key``, which
   Dagster itself refuses to run twice, so the state file is no longer needed.

Everything else the watcher did is now Dagster's job: the queue becomes the
run queue, the retry loop becomes a run retry policy, the ``.log`` beside the
failed file becomes the run's logs, and the success / failure hooks become
run-status sensors.

The file filtering and the snapshot-date parsing are imported from
``watch_inventory`` rather than reimplemented, so both paths agree.
"""
from __future__ import annotations

import json

from dagster import (
    DefaultSensorStatus,
    RunRequest,
    SensorEvaluationContext,
    SensorResult,
    SkipReason,
    sensor,
)

from watch_inventory import file_sha256, is_inventory_file

from .jobs import classification_reload_job, inventory_refresh_job, rop_reload_job
from .resources import RopProject

# Only consider a file ready once its size and mtime are unchanged for a
# whole tick. With a 30 s interval that is the watcher's 5 s settle window,
# more conservatively.
SENSOR_INTERVAL_SECONDS = 30

# Guard against a run per file when someone dumps a year of exports at once.
MAX_RUNS_PER_TICK = 3


def _load_cursor(raw: str | None) -> dict[str, list[int]]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


@sensor(
    name="inbox_sensor",
    job=inventory_refresh_job,
    minimum_interval_seconds=SENSOR_INTERVAL_SECONDS,
    default_status=DefaultSensorStatus.STOPPED,
    description="Launch an inventory refresh for every new export dropped in inbox/.",
)
def inbox_sensor(context: SensorEvaluationContext, project: RopProject) -> SensorResult:
    project.ensure_inbox()
    previous = _load_cursor(context.cursor)
    current: dict[str, list[int]] = {}
    requests: list[RunRequest] = []
    settling: list[str] = []

    files = [p for p in project.inbox.iterdir() if p.is_file() and is_inventory_file(p)]
    for path in sorted(files, key=lambda p: p.stat().st_mtime):
        try:
            st = path.stat()
        except FileNotFoundError:  # picked up by a run between iterdir and stat
            continue
        signature = [st.st_size, st.st_mtime_ns]
        current[path.name] = signature

        if previous.get(path.name) != signature or st.st_size == 0:
            settling.append(path.name)  # still arriving; look again next tick
            continue
        if len(requests) >= MAX_RUNS_PER_TICK:
            continue

        try:
            digest = file_sha256(path)
        except (PermissionError, OSError) as exc:  # still locked by the writer
            context.log.info("%s is not readable yet (%s); retrying next tick", path.name, exc)
            settling.append(path.name)
            continue

        context.log.info("Requesting a refresh for %s", path.name)
        requests.append(
            RunRequest(
                # Dagster will not launch the same run_key twice, so identical
                # content is skipped the way the watcher's state file did it.
                run_key=digest,
                run_config={
                    "ops": {
                        "inventory_snapshot": {
                            "config": {"inventory_file": str(path), "archive": True}
                        }
                    }
                },
                tags={"inventory_file": path.name, "sha256": digest[:12]},
            )
        )

    # The cursor must be written on every tick, including a skip - it is what
    # records that a file has been seen at this size. Returning a bare
    # SkipReason discards the cursor, and the file then looks "still settling"
    # for ever, so a skip is expressed as a SensorResult carrying both.
    if requests:
        skip = None
    elif settling:
        skip = SkipReason(f"{len(settling)} file(s) still settling: {', '.join(settling)}")
    else:
        skip = SkipReason(f"No new inventory export in {project.inbox.name}/")

    return SensorResult(run_requests=requests, cursor=json.dumps(current), skip_reason=skip)


# --------------------------------------------------------------------------
# Source files edited in place: the ROP workbook and the business-owned lists
# --------------------------------------------------------------------------
SOURCE_SENSOR_INTERVAL_SECONDS = 60


def _source_groups(project: RopProject) -> dict[str, list]:
    """Job to launch -> the files whose change needs it. rop_reload_job ends in
    warehouse_db, so it also re-applies the category and VED lists."""
    return {
        rop_reload_job.name: [project.rop_path, project.abc_xyz_path],
        classification_reload_job.name: [project.category_path, project.ved_path],
    }


@sensor(
    name="source_files_sensor",
    jobs=[rop_reload_job, classification_reload_job],
    minimum_interval_seconds=SOURCE_SENSOR_INTERVAL_SECONDS,
    default_status=DefaultSensorStatus.RUNNING,
    description="Reload the warehouse when ROP final.xlsb, ABC XYZ.xlsx, SKU category.csv or VED ангилал.csv is saved.",
)
def source_files_sensor(context: SensorEvaluationContext, project: RopProject) -> SensorResult:
    """Like inbox_sensor, a file counts once its (size, mtime) held still for a
    whole tick, so a workbook Excel is still writing is never read. The
    cursor keeps, per file, the signature last seen and the one last loaded.
    The first tick only records the files: starting Dagster does not reload."""
    cursor = _load_cursor(context.cursor)
    seen, loaded = cursor.get("seen", {}), cursor.get("loaded", {})
    new_seen: dict[str, list[int]] = {}
    changed: dict[str, list[str]] = {}
    settling: list[str] = []

    for job_name, paths in _source_groups(project).items():
        for path in paths:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            signature = [st.st_size, st.st_mtime_ns]
            new_seen[path.name] = signature
            if path.name not in loaded:
                loaded[path.name] = signature  # baseline
            elif signature == loaded[path.name]:
                continue
            elif seen.get(path.name) != signature or st.st_size == 0:
                settling.append(path.name)
            else:
                changed.setdefault(job_name, []).append(path.name)
                loaded[path.name] = signature

    if rop_reload_job.name in changed:  # it rebuilds warehouse_db too
        changed.pop(classification_reload_job.name, None)
    requests = [
        RunRequest(
            job_name=job_name,
            run_key=f"{job_name}:" + "|".join(f"{n}:{loaded[n][1]}" for n in sorted(names)),
            tags={"changed_files": ", ".join(names)},
        )
        for job_name, names in changed.items()
    ]
    for request in requests:
        context.log.info("%s changed; launching %s", request.tags["changed_files"], request.job_name)

    if requests:
        skip = None
    elif settling:
        skip = SkipReason(f"Still being saved: {', '.join(settling)}")
    else:
        skip = SkipReason("No source file changed")
    return SensorResult(run_requests=requests, cursor=json.dumps({"seen": new_seen, "loaded": loaded}), skip_reason=skip)


inventory_sensors = [inbox_sensor, source_files_sensor]
