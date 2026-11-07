"""Draft jobs and schedules."""
from __future__ import annotations

from dagster import AssetSelection, ScheduleDefinition, define_asset_job

from .assets import sources_partitions

_sync = AssetSelection.assets("sharepoint_delta_sync")

first_load_job = define_asset_job(
    "first_load", _sync, partitions_def=sources_partitions, tags={"kb/run_kind": "FIRST_LOAD"}
)
daily_delta_job = define_asset_job(
    "daily_delta", _sync, partitions_def=sources_partitions, tags={"kb/run_kind": "DELTA"}
)
# GAP: reconcile should ignore the token and diff a full listing against the registry
weekly_reconcile_job = define_asset_job(
    "weekly_reconcile", _sync, partitions_def=sources_partitions, tags={"kb/run_kind": "RECONCILE"}
)
# GAP: dry_run (list + classify, no writes) and retention_purge are not drafted.

daily_delta_schedule = ScheduleDefinition(job=daily_delta_job, cron_schedule="0 2 * * *")
weekly_reconcile_schedule = ScheduleDefinition(job=weekly_reconcile_job, cron_schedule="0 4 * * 0")
