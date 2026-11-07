"""Dagster definitions: thin wrappers around IngestEngine.

Run locally with `kb.ps1 up` (service "dagster": daemon + webserver on 127.0.0.1:3070).
Run-queue limits live in infra/local/dagster/dagster.yaml: one run per source (tag kb/source), at most
`max_concurrent_ingestion_runs` overall. The registry independently enforces one in-flight run per source.
"""

# NOTE: no `from __future__ import annotations` here: Dagster resolves Config types from runtime annotations.

from typing import Any

import dagster as dg

from kb.config import load_settings
from kb.ingest import work
from kb.ingest.engine import IngestEngine
from kb.ingest.factory import build_engine

_engines: dict[tuple[Any, ...], IngestEngine] = {}


class KbIngest(dg.ConfigurableResource):  # type: ignore[type-arg]
    """Builds (once per process) the engine from config; optional overrides are for tests."""

    env: str = "local"
    registry_dsn: str | None = None
    index_alias: str | None = None
    raw_url: str | None = None
    processed_url: str | None = None
    sharepoint_root: str | None = None

    def engine(self) -> IngestEngine:
        key = (self.env, self.registry_dsn, self.index_alias, self.raw_url, self.processed_url, self.sharepoint_root)
        if key not in _engines:
            eng = build_engine(
                load_settings(self.env, strict=True),
                registry_dsn=self.registry_dsn,
                index_alias=self.index_alias,
                raw_url=self.raw_url,
                processed_url=self.processed_url,
                sharepoint_root=self.sharepoint_root,
            )
            eng.index.ensure_index()
            _engines[key] = eng
        return _engines[key]


# ---------------------------------------------------------------------------------------------- configs
class DryRunConfig(dg.Config):
    source_id: str
    revision_id: str


class SyncConfig(dg.Config):
    source_id: str
    kind: str = "DELTA"  # FIRST_LOAD | DELTA | RECONCILE


class UploadConfig(dg.Config):
    version_id: str


class ReprocessConfig(dg.Config):
    exception_id: str


# ---------------------------------------------------------------------------------------------- ops / jobs
@dg.op
def dry_run_op(context: dg.OpExecutionContext, config: DryRunConfig, kb: KbIngest) -> dict[str, Any]:
    out = kb.engine().dry_run(config.source_id, config.revision_id)
    context.log.info(f"dry run {config.source_id}: {out['counts']}")
    return out


@dg.op
def sync_op(context: dg.OpExecutionContext, config: SyncConfig, kb: KbIngest) -> dict[str, Any]:
    out = kb.engine().sync(config.source_id, config.kind)
    context.log.info(f"{config.kind} {config.source_id}: {out['counts']}")
    return out


@dg.op
def upload_op(context: dg.OpExecutionContext, config: UploadConfig, kb: KbIngest) -> dict[str, Any]:
    return kb.engine().process_upload(config.version_id)


@dg.op
def reprocess_op(context: dg.OpExecutionContext, config: ReprocessConfig, kb: KbIngest) -> dict[str, Any]:
    return kb.engine().reprocess_released(config.exception_id)


@dg.op
def index_sync_op(context: dg.OpExecutionContext, kb: KbIngest) -> dict[str, Any]:
    return kb.engine().index_sync()


@dg.op
def purge_op(context: dg.OpExecutionContext, kb: KbIngest) -> dict[str, Any]:
    return kb.engine().purge()


@dg.job(tags={"kb/run_kind": "DRY_RUN"})
def dry_run_job() -> None:
    dry_run_op()


@dg.job(tags={"kb/run_kind": "SYNC"})
def sync_job() -> None:
    sync_op()


@dg.job(tags={"kb/run_kind": "UPLOAD"})
def upload_job() -> None:
    upload_op()


@dg.job(tags={"kb/run_kind": "REPROCESS"})
def reprocess_job() -> None:
    reprocess_op()


@dg.job(tags={"kb/run_kind": "INDEX_SYNC", "kb/source": "_index"})
def index_sync_job() -> None:
    index_sync_op()


@dg.job(tags={"kb/run_kind": "PURGE", "kb/source": "_purge"})
def purge_job() -> None:
    purge_op()


def _src_tags(source_id: str) -> dict[str, str]:
    return {"kb/source": source_id}


# ---------------------------------------------------------------------------------------------- sensors
ON = dg.DefaultSensorStatus.RUNNING


@dg.sensor(job=dry_run_job, minimum_interval_seconds=30, default_status=ON)
def dry_run_sensor(context: dg.SensorEvaluationContext, kb: KbIngest) -> Any:
    rows = work.pending_dry_runs(kb.engine())
    if not rows:
        return dg.SkipReason("no submitted revision without a dry run")
    return [
        dg.RunRequest(
            run_key=f"dry:{r['revision_id']}",
            tags=_src_tags(r["source_id"]),
            run_config=dg.RunConfig(ops={"dry_run_op": DryRunConfig(**r)}),
        )
        for r in rows
    ]


@dg.sensor(job=sync_job, minimum_interval_seconds=30, default_status=ON)
def first_load_sensor(context: dg.SensorEvaluationContext, kb: KbIngest) -> Any:
    ids = work.sources_needing_first_load(kb.engine())
    if not ids:
        return dg.SkipReason("no newly approved source")
    return [
        dg.RunRequest(
            run_key=f"first:{sid}",
            tags=_src_tags(sid),
            run_config=dg.RunConfig(ops={"sync_op": SyncConfig(source_id=sid, kind="FIRST_LOAD")}),
        )
        for sid in ids
    ]


@dg.sensor(job=upload_job, minimum_interval_seconds=30, default_status=ON)
def upload_sensor(context: dg.SensorEvaluationContext, kb: KbIngest) -> Any:
    rows = work.received_uploads(kb.engine())
    if not rows:
        return dg.SkipReason("no RECEIVED upload")
    return [
        dg.RunRequest(
            run_key=f"upload:{r['version_id']}",
            tags=_src_tags(r["source_id"]),
            run_config=dg.RunConfig(ops={"upload_op": UploadConfig(version_id=r["version_id"])}),
        )
        for r in rows
    ]


@dg.sensor(job=reprocess_job, minimum_interval_seconds=30, default_status=ON)
def released_exception_sensor(context: dg.SensorEvaluationContext, kb: KbIngest) -> Any:
    rows = work.released_exceptions(kb.engine())
    if not rows:
        return dg.SkipReason("no released exception waiting")
    return [
        dg.RunRequest(
            run_key=f"reprocess:{r['exception_id']}",
            tags=_src_tags(r["source_id"]),
            run_config=dg.RunConfig(ops={"reprocess_op": ReprocessConfig(exception_id=r["exception_id"])}),
        )
        for r in rows
    ]


@dg.sensor(job=index_sync_job, minimum_interval_seconds=30, default_status=ON)
def index_sync_sensor(context: dg.SensorEvaluationContext, kb: KbIngest) -> Any:
    drift = work.index_drift(kb.engine())
    if not drift:
        return dg.SkipReason("index converged")
    return dg.RunRequest(run_key=f"index_sync:{work.drift_key(drift)}")


# ---------------------------------------------------------------------------------------------- schedules
@dg.schedule(
    job=sync_job,
    cron_schedule="0 2 * * *",
    execution_timezone="Asia/Singapore",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def daily_delta(context: dg.ScheduleEvaluationContext, kb: KbIngest) -> Any:
    return [
        dg.RunRequest(
            run_key=f"delta:{sid}:{context.scheduled_execution_time:%Y%m%d}",
            tags=_src_tags(sid),
            run_config=dg.RunConfig(ops={"sync_op": SyncConfig(source_id=sid, kind="DELTA")}),
        )
        for sid in work.active_sharepoint_sources(kb.engine())
    ]


@dg.schedule(
    job=sync_job,
    cron_schedule="0 4 * * 0",
    execution_timezone="Asia/Singapore",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)
def weekly_reconcile(context: dg.ScheduleEvaluationContext, kb: KbIngest) -> Any:
    return [
        dg.RunRequest(
            run_key=f"reconcile:{sid}:{context.scheduled_execution_time:%Y%m%d}",
            tags=_src_tags(sid),
            run_config=dg.RunConfig(ops={"sync_op": SyncConfig(source_id=sid, kind="RECONCILE")}),
        )
        for sid in work.active_sharepoint_sources(kb.engine())
    ]


daily_purge = dg.ScheduleDefinition(
    job=purge_job,
    cron_schedule="0 3 * * *",
    execution_timezone="Asia/Singapore",
    default_status=dg.DefaultScheduleStatus.RUNNING,
)

defs = dg.Definitions(
    jobs=[dry_run_job, sync_job, upload_job, reprocess_job, index_sync_job, purge_job],
    sensors=[dry_run_sensor, first_load_sensor, upload_sensor, released_exception_sensor, index_sync_sensor],
    schedules=[daily_delta, weekly_reconcile, daily_purge],
    resources={"kb": KbIngest()},
)
