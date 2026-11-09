"""Draft SharePoint delta sync asset (one dynamic partition per source)."""
from __future__ import annotations

import hashlib

from dagster import AssetExecutionContext, DynamicPartitionsDefinition, MaterializeResult, asset

from .resources import GRAPH, GraphClient, RegistryResource, SearchResource

sources_partitions = DynamicPartitionsDefinition(name="kb_source")


def doc_id_for(source_id: str, item_id: str) -> str:
    return hashlib.sha256(f"{source_id}|{item_id}".encode()).hexdigest()[:32]


def classify(item: dict, folder_path: str) -> str:
    if "deleted" in item:
        return "deleted"
    parent = item.get("parentReference", {}).get("path", "")
    if folder_path not in parent:
        return "out_of_scope"  # moved outside the onboarded folder
    if "folder" in item:
        return "folder"
    return "file"


@asset(partitions_def=sources_partitions)
def sharepoint_delta_sync(
    context: AssetExecutionContext,
    graph: GraphClient,
    registry: RegistryResource,
    search: SearchResource,
) -> MaterializeResult:
    source_id = context.partition_key
    with registry.connect() as conn:
        src = conn.execute(
            "SELECT s.sp_drive_id, s.sp_folder_path, r.max_file_bytes, r.allowed_extensions, r.acl_groups "
            "FROM kb.kb_source s JOIN kb.source_revision r ON r.revision_id = s.current_revision_id "
            "WHERE s.source_id = %s AND s.status = 'ACTIVE'",
            (source_id,),
        ).fetchone()
        if src is None:
            raise RuntimeError(f"source {source_id} is not ACTIVE")
        drive_id, folder, max_bytes, exts, _acl = src
        token = conn.execute(
            "SELECT delta_token FROM kb.sync_state WHERE source_id = %s", (source_id,)
        ).fetchone()

    url = token[0] if token and token[0] else f"{GRAPH}/drives/{drive_id}/root/delta"
    stats = {"new_or_changed": 0, "deleted": 0, "out_of_scope": 0, "skipped": 0}
    next_token = None
    while url:
        try:
            page = graph.get(url)
        except Exception as exc:  # GAP: 410 resyncRequired must trigger a full resync
            context.log.error(f"delta failed: {exc}")
            raise
        for item in page.get("value", []):
            kind = classify(item, folder)
            did = doc_id_for(source_id, item["id"])
            if kind == "deleted":
                stats["deleted"] += 1
                # GAP: registry flipped before ES; must hide in ES first
                with registry.connect() as conn:
                    conn.execute(
                        "UPDATE kb.document SET status='DELETED', deleted_at=now() WHERE doc_id=%s", (did,)
                    )
                search.client().update_by_query(
                    index=search.index_alias,
                    query={"term": {"doc_id": did}},
                    script={"source": "ctx._source.is_current=false"},
                )
            elif kind == "out_of_scope":
                stats["out_of_scope"] += 1
            elif kind == "file":
                name = item["name"]
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext not in exts or item.get("size", 0) > max_bytes:
                    stats["skipped"] += 1  # GAP: must raise an exception_item instead
                    continue
                data = graph.download(drive_id, item["id"])
                context.log.info(f"{name}: {len(data)} bytes, cTag={item.get('cTag')}")
                stats["new_or_changed"] += 1
                # GAP: land raw, parse, PII gate, chunk, embed, index, version flip
        url = page.get("@odata.nextLink")
        next_token = page.get("@odata.deltaLink", next_token)

    # the token is committed only after the whole delta succeeded
    with registry.connect() as conn:
        conn.execute(
            "INSERT INTO kb.sync_state (source_id, delta_token, last_success_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (source_id) DO UPDATE SET delta_token = EXCLUDED.delta_token, last_success_at = now()",
            (source_id, next_token),
        )
    return MaterializeResult(metadata=stats)
