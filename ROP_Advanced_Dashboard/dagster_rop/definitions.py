"""Dagster entry point.

    dagster dev -m dagster_rop.definitions      (or: run_dagster.bat)

Resources can be overridden without touching the code, e.g. to point at a
copy of the project or to turn on the Power BI CSV export:

    DAGSTER_ROP_PROJECT_DIR=D:\\rop_test
    DAGSTER_ROP_POWERBI=true
"""
from __future__ import annotations

import os
from pathlib import Path

from dagster import Definitions, load_assets_from_modules

from . import assets as assets_module
from .checks import warehouse_checks
from .jobs import rop_jobs, rop_schedules
from .resources import PROJECT_DIR, DuckDBWarehouse, RopProject
from .sensors import inventory_sensors


def _project_dir() -> str:
    return os.environ.get("DAGSTER_ROP_PROJECT_DIR", str(PROJECT_DIR))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


project = RopProject(
    project_dir=_project_dir(),
    powerbi_export=_flag("DAGSTER_ROP_POWERBI"),
    missing_as_zero=_flag("DAGSTER_ROP_MISSING_AS_ZERO"),
    excess_threshold=float(os.environ.get("DAGSTER_ROP_EXCESS_THRESHOLD", "2.0")),
)

unified_db = DuckDBWarehouse(database=str(Path(_project_dir()) / "data" / "warehouse.duckdb"))

defs = Definitions(
    assets=load_assets_from_modules([assets_module]),
    asset_checks=warehouse_checks,
    jobs=rop_jobs,
    schedules=rop_schedules,
    sensors=inventory_sensors,
    resources={"project": project, "unified_db": unified_db},
)
