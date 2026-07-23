import os
from dataclasses import dataclass
from typing import Optional

from supabase import Client, create_client

DEFAULT_CORE_SCHEMA = os.getenv("UK_AQ_CORE_SCHEMA", "uk_aq_core")
DEFAULT_RAW_SCHEMA = os.getenv("UK_AQ_RAW_SCHEMA", "uk_aq_raw")
DEFAULT_POP_SCHEMA = os.getenv("UK_AQ_POP_SCHEMA", "uk_aq_pop")


def _resolve_schema_client(client: Client, schema: str) -> Client:
    if not schema or schema == "public":
        return client
    if hasattr(client, "schema"):
        return client.schema(schema)
    postgrest = getattr(client, "postgrest", None)
    if postgrest is not None and hasattr(postgrest, "schema"):
        return postgrest.schema(schema)
    raise RuntimeError("Supabase client does not support schema selection.")


class SchemaClient:
    def __init__(self, client: Client, schema: str) -> None:
        self._client = _resolve_schema_client(client, schema)

    def table(self, name: str):
        if hasattr(self._client, "table"):
            return self._client.table(name)
        if hasattr(self._client, "from_"):
            return self._client.from_(name)
        raise RuntimeError("Supabase client does not expose table/from_ methods.")

    def rpc(self, fn: str, params: Optional[dict] = None):
        if hasattr(self._client, "rpc"):
            return self._client.rpc(fn, params)
        raise RuntimeError("Supabase client does not expose rpc.")


@dataclass
class SupabaseSchemas:
    client: Client
    core: SchemaClient
    raw: SchemaClient
    pop: SchemaClient
    public: SchemaClient

    @classmethod
    def from_client(
        cls,
        client: Client,
        core_schema: str = DEFAULT_CORE_SCHEMA,
        raw_schema: str = DEFAULT_RAW_SCHEMA,
        pop_schema: str = DEFAULT_POP_SCHEMA,
    ) -> "SupabaseSchemas":
        return cls(
            client=client,
            core=SchemaClient(client, core_schema),
            raw=SchemaClient(client, raw_schema),
            pop=SchemaClient(client, pop_schema),
            public=SchemaClient(client, "public"),
        )


def create_supabase_client(
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
) -> Client:
    supabase_url = supabase_url or os.getenv("SUPABASE_URL")
    supabase_key = supabase_key or os.getenv("SB_SECRET_KEY") or os.getenv(
        "SUPABASE_KEY"
    )
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL and SB_SECRET_KEY are required.")
    return create_client(supabase_url, supabase_key)
