"""Jobs and schedules - the named entry points into the asset graph."""
from __future__ import annotations

from dagster import (
    AssetSelection,
    DefaultScheduleStatus,
    RetryPolicy,
    ScheduleDefinition,
    define_asset_job,
)

# A new export arrived: refresh the status and batch expiry facts, rebuild
# history and the unified database, then run the checks. Does not re-read the
# ROP workbook.
inventory_refresh_job = define_asset_job(
    name="inventory_refresh_job",
    selection=AssetSelection.assets("inventory_snapshot", "batch_expiry", "status_history", "warehouse_db"),
    description="Load one inventory export and rebuild the unified database.",
    # A refresh is idempotent - it rewrites the same tables - so a transient
    # failure (a file still locked by Excel, a DuckDB rename race) can retry.
    op_retry_policy=RetryPolicy(max_retries=2, delay=30),
)

# A new ROP workbook arrived: reload branch ROP, then everything downstream.
# batch_expiry re-projects the current batches against the new demand.
rop_reload_job = define_asset_job(
    name="rop_reload_job",
    selection=AssetSelection.assets("branch_rop", "batch_expiry", "status_history", "warehouse_db"),
    description="Reload the branch ROP workbook and rebuild the unified database.",
    op_retry_policy=RetryPolicy(max_retries=1, delay=30),
)

# «SKU category.csv» or «VED ангилал.csv» changed: warehouse_db rebuilds the
# dimensions from both files and re-applies the VED to every fact.
classification_reload_job = define_asset_job(
    name="classification_reload_job",
    selection=AssetSelection.assets("warehouse_db"),
    description="Re-apply SKU category.csv and VED ангилал.csv and rebuild the unified database.",
    op_retry_policy=RetryPolicy(max_retries=1, delay=30),
)

# Everything, in dependency order.
full_rebuild_job = define_asset_job(
    name="full_rebuild_job",
    selection=AssetSelection.assets(
        "branch_rop", "inventory_snapshot", "batch_expiry", "status_history", "warehouse_db"
    ),
    description="Full rebuild: ROP workbook, the newest export, history and the unified database.",
)

# Just the checks, against the warehouse as it stands.
warehouse_check_job = define_asset_job(
    name="warehouse_check_job",
    selection=AssetSelection.checks_for_assets("warehouse_db"),
    description="Run the integrity, mapping-rate and freshness checks without rebuilding anything.",
)

# Weekday morning: confirm the warehouse is still sound and the newest
# snapshot is not stale, before anyone opens the dashboard.
daily_check_schedule = ScheduleDefinition(
    name="daily_warehouse_check",
    job=warehouse_check_job,
    cron_schedule="0 7 * * 1-5",
    execution_timezone="Asia/Ulaanbaatar",
    default_status=DefaultScheduleStatus.STOPPED,
    description="Weekdays 07:00 Ulaanbaatar - integrity, mapping rate and snapshot freshness.",
)

rop_jobs = [inventory_refresh_job, rop_reload_job, classification_reload_job, full_rebuild_job, warehouse_check_job]
rop_schedules = [daily_check_schedule]
