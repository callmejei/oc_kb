"""Draft Definitions.

Run-queue limits belong in dagster.yaml:

    run_queue:
      max_concurrent_runs: 4
      tag_concurrency_limits:
        - key: kb/source
          value: {applyLimitPerUniqueValue: true}
          limit: 1
"""
from __future__ import annotations

from dagster import Definitions, EnvVar

from .assets import sharepoint_delta_sync
from .jobs import (
    daily_delta_job,
    daily_delta_schedule,
    first_load_job,
    weekly_reconcile_job,
    weekly_reconcile_schedule,
)
from .resources import GraphClient, RegistryResource, SearchResource
from .sensors import approved_source_sensor

defs = Definitions(
    assets=[sharepoint_delta_sync],
    jobs=[first_load_job, daily_delta_job, weekly_reconcile_job],
    schedules=[daily_delta_schedule, weekly_reconcile_schedule],
    sensors=[approved_source_sensor],
    resources={
        "graph": GraphClient(
            tenant_id=EnvVar("GRAPH_TENANT_ID"),
            client_id=EnvVar("GRAPH_CLIENT_ID"),
            client_secret=EnvVar("GRAPH_CLIENT_SECRET"),
        ),
        "registry": RegistryResource(dsn=EnvVar("KB_REGISTRY_DSN")),
        "search": SearchResource(url=EnvVar("KB_ES_URL")),
    },
)
