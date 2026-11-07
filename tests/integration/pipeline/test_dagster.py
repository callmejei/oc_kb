"""Dagster wiring: run-queue limits really load; sensors emit the right run requests; a job executes."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import dagster as dg
import pytest
from dagster._core.instance.ref import InstanceRef

from kb.config import REPO_ROOT
from kb.ingest import definitions as d

from helpers import Api, onboard, register

YAML = REPO_ROOT / "infra" / "local" / "dagster" / "dagster.yaml"


def test_run_queue_limits_are_in_force(tmp_path: Path):
    shutil.copyfile(YAML, tmp_path / "dagster.yaml")
    inst = dg.DagsterInstance.from_ref(InstanceRef.from_dir(str(tmp_path)))
    try:
        cfg = inst.get_concurrency_config().run_queue_config
        assert cfg.max_concurrent_runs == 4
        assert {"key": "kb/source", "value": {"applyLimitPerUniqueValue": True}, "limit": 1} in list(
            cfg.tag_concurrency_limits
        )
        assert inst.run_coordinator.__class__.__name__ == "QueuedRunCoordinator"
    finally:
        inst.dispose()


def test_definitions_load():
    names = {j.name for j in d.defs.resolve_all_job_defs()}
    assert {"dry_run_job", "sync_job", "upload_job", "reprocess_job", "index_sync_job", "purge_job"} <= names


@pytest.fixture(scope="module")
def kb_res(tmp_path_factory, pipe_dsn, data_dir):
    root = tmp_path_factory.mktemp("dagster")
    shutil.copytree(data_dir / "sharepoint", root / "sharepoint")
    res = d.KbIngest(
        registry_dsn=pipe_dsn,
        index_alias=f"kb-test-{uuid.uuid4().hex[:8]}",
        raw_url=(root / "raw").as_uri(),
        processed_url=(root / "processed").as_uri(),
        sharepoint_root=str(root / "sharepoint"),
    )
    yield res
    res.engine().index.drop()  # type: ignore[attr-defined]


def run_requests(sensor: dg.SensorDefinition, res: d.KbIngest) -> list[dg.RunRequest]:
    ctx = dg.build_sensor_context(resources={"kb": res})
    out = sensor(ctx)
    if isinstance(out, dg.SkipReason) or out is None:
        return []
    return [out] if isinstance(out, dg.RunRequest) else list(out)


def test_dry_run_then_first_load_sensors(api: Api, kb_res):
    created = register(api, "shared", sp_site_id="shared")
    rid = created["revision"]["revision_id"]
    req = api.call("POST", f"/revisions/{rid}/submit", "shr.maker", {}).json()["request_id"]
    reqs = {r.run_key: r for r in run_requests(d.dry_run_sensor, kb_res)}
    assert f"dry:{rid}" in reqs and reqs[f"dry:{rid}"].tags["kb/source"] == created["source"]["source_id"]
    res = d.dry_run_job.execute_in_process(run_config=reqs[f"dry:{rid}"].run_config, resources={"kb": kb_res})
    assert res.success
    assert f"dry:{rid}" not in {r.run_key for r in run_requests(d.dry_run_sensor, kb_res)}  # report exists now
    api.call("POST", f"/requests/{req}/approve", "shr.checker", {})
    first = [
        r for r in run_requests(d.first_load_sensor, kb_res) if r.run_key == f"first:{created['source']['source_id']}"
    ]
    assert len(first) == 1


def test_upload_sensor_and_job_execute(api: Api, kb_res, client, as_admin):
    space = onboard(api, "retail", "UPLOAD")["source_id"]
    eng = kb_res.engine()
    from fastapi.testclient import TestClient

    from kb.adapters.local.identity import YamlIdentityProvider
    from kb.config import load_settings
    from kb.gateway.app import create_app

    app = create_app(
        load_settings("local"),
        client.app.state.kb.registry,
        YamlIdentityProvider("config/users.yaml"),
        trusted_clients=frozenset({"testclient"}),
        raw_store=eng.raw,
        chunk_index=eng.index,
    )
    with TestClient(app) as c:
        p = REPO_ROOT / "data" / "upload_fixtures" / "publish_approved" / "Branch_Queue_Management_Tips.pdf"
        with open(p, "rb") as f:
            up = c.post(
                f"/api/v1/uploads/{space}", headers={"X-KB-User": "ret.user2"}, files={"file": (p.name, f)}
            ).json()
    reqs = {r.run_key: r for r in run_requests(d.upload_sensor, kb_res)}
    rr = reqs[f"upload:{up['version_id']}"]
    assert rr.tags["kb/source"] == space
    res = d.upload_job.execute_in_process(run_config=rr.run_config, resources={"kb": kb_res})
    assert res.success
    with as_admin() as c2:
        st = c2.execute(
            "SELECT status, is_current FROM kb.document_version WHERE version_id = %s", (up["version_id"],)
        ).fetchone()
    assert st == {"status": "INDEXED", "is_current": True}
    assert f"upload:{up['version_id']}" not in {r.run_key for r in run_requests(d.upload_sensor, kb_res)}
