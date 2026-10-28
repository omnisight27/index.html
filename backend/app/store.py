"""Credential storage.

Production: Supabase Postgres `user_credentials` table, reached through
PostgREST with the SERVICE-ROLE key (server-side only — it bypasses RLS, which
is exactly why it must never ship to the frontend). The table has RLS enabled
with no anon/authenticated policies, so the public anon key cannot touch it.

Tests: InMemoryStore with the same interface.
"""
import os
from datetime import datetime, timezone

import httpx


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryStore:
    def __init__(self):
        self.rows = {}  # (user_id, provider) -> row dict

    def upsert(self, user_id, provider, key_name_masked, credentials_enc):
        row = {
            "user_id": user_id,
            "provider": provider,
            "key_name_masked": key_name_masked,
            "credentials_enc": credentials_enc,
            "updated_at": _now_iso(),
        }
        self.rows[(user_id, provider)] = row
        return row

    def get(self, user_id, provider):
        return self.rows.get((user_id, provider))

    def delete(self, user_id, provider):
        return self.rows.pop((user_id, provider), None) is not None


class SupabaseStore:
    """PostgREST-backed store. `transport` is injectable for tests."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.base = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        if self.base and not self.base.startswith(("http://", "https://")):
            self.base = "https://" + self.base
        self.service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.base or not self.service_key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set")
        self.client = httpx.Client(transport=transport, timeout=15.0)

    def _headers(self, extra=None):
        h = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    @property
    def _table_url(self):
        return f"{self.base}/rest/v1/user_credentials"

    def upsert(self, user_id, provider, key_name_masked, credentials_enc):
        row = {
            "user_id": user_id,
            "provider": provider,
            "key_name_masked": key_name_masked,
            "credentials_enc": credentials_enc,
            "updated_at": _now_iso(),
        }
        r = self.client.post(
            self._table_url,
            json=row,
            headers=self._headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
        )
        r.raise_for_status()
        data = r.json()
        return data[0] if data else row

    def get(self, user_id, provider):
        r = self.client.get(
            self._table_url,
            params={
                "user_id": f"eq.{user_id}",
                "provider": f"eq.{provider}",
                "select": "user_id,provider,key_name_masked,credentials_enc,created_at,updated_at",
            },
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None

    def delete(self, user_id, provider):
        r = self.client.delete(
            self._table_url,
            params={"user_id": f"eq.{user_id}", "provider": f"eq.{provider}"},
            headers=self._headers({"Prefer": "return=representation"}),
        )
        r.raise_for_status()
        return len(r.json()) > 0
