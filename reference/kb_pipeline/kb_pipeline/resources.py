"""Draft resources. Direct clients; Phase 2 replaces these with port adapters."""
from __future__ import annotations

import msal
import psycopg
import requests
from dagster import ConfigurableResource
from elasticsearch import Elasticsearch

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphClient(ConfigurableResource):
    tenant_id: str
    client_id: str
    client_secret: str  # from the CML secret store; never committed

    def _token(self) -> str:
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        res = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        return res["access_token"]

    def get(self, url: str) -> dict:
        r = requests.get(url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=60)
        r.raise_for_status()
        return r.json()

    def download(self, drive_id: str, item_id: str) -> bytes:
        r = requests.get(
            f"{GRAPH}/drives/{drive_id}/items/{item_id}/content",
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=300,
        )
        r.raise_for_status()
        return r.content  # GAP: whole file in memory


class RegistryResource(ConfigurableResource):
    dsn: str

    def connect(self, actor_id: str = "svc:pipeline") -> psycopg.Connection:
        conn = psycopg.connect(self.dsn)
        conn.execute("SELECT set_config('kb.actor_id', %s, false)", (actor_id,))
        return conn


class SearchResource(ConfigurableResource):
    url: str
    index_alias: str = "kb-chunks"

    def client(self) -> Elasticsearch:
        return Elasticsearch(self.url)
