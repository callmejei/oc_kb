"""Draft sensors."""
from __future__ import annotations

from dagster import RunRequest, SensorEvaluationContext, SkipReason, sensor

from .jobs import first_load_job
from .resources import RegistryResource


@sensor(job=first_load_job, minimum_interval_seconds=60)
def approved_source_sensor(context: SensorEvaluationContext, registry: RegistryResource):
    """Starts a first load for each source that became ACTIVE and has no sync_state yet."""
    with registry.connect() as conn:
        rows = conn.execute(
            "SELECT s.source_id FROM kb.kb_source s LEFT JOIN kb.sync_state t USING (source_id) "
            "WHERE s.status = 'ACTIVE' AND t.source_id IS NULL"
        ).fetchall()
    if not rows:
        yield SkipReason("no newly approved sources")
        return
    for (source_id,) in rows:
        context.instance.add_dynamic_partitions("kb_source", [source_id])
        yield RunRequest(
            run_key=f"first_load:{source_id}", partition_key=source_id, tags={"kb/source": source_id}
        )


# GAP: upload_sensor (RECEIVED uploads -> gates -> processing) is not drafted; see CLAUDE.md s5.
