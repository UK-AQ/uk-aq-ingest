#!/usr/bin/env python3
"""Repair obs_aqidb uk_aq_core.timeseries IDs to match ingestdb canonical IDs.

Default mode is dry-run. Pass --apply to execute changes.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


class RepairError(RuntimeError):
    """Fatal repair error."""


@dataclass(frozen=True)
class TsRow:
    connector_code: str
    service_ref: str
    timeseries_ref: str
    ts_id: int


@dataclass(frozen=True)
class Mismatch:
    connector_code: str
    service_ref: str
    timeseries_ref: str
    src_id: int
    dst_id: int


@dataclass(frozen=True)
class TableRef:
    schema_name: str
    table_name: str

    def fq(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair obs_aqidb timeseries numeric IDs to match ingestdb canonical IDs "
            "using natural key (connector_code, service_ref, timeseries_ref)."
        )
    )
    parser.add_argument(
        "--source-db-url",
        help="Ingest Postgres URL. Defaults to SUPABASE_DB_URL.",
    )
    parser.add_argument(
        "--dest-db-url",
        help="Obs AQI DB Postgres URL. Defaults to OBS_AQIDB_SUPABASE_DB_URL.",
    )
    parser.add_argument(
        "--connector-code",
        action="append",
        default=[],
        help=(
            "Optional connector_code filter. Repeat flag for multiple codes. "
            "Default: all connectors."
        ),
    )
    parser.add_argument(
        "--allow-key-delta",
        action="store_true",
        help=(
            "Allow apply even when natural-key sets differ between source and destination. "
            "By default, apply aborts unless key sets match exactly."
        ),
    )
    parser.add_argument(
        "--report-limit",
        type=int,
        default=30,
        help="Max sample rows printed per mismatch category (default: 30).",
    )
    parser.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=15000,
        help="Destination transaction lock_timeout in milliseconds (default: 15000).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply writes. Default is dry-run.",
    )
    return parser.parse_args()


def env_or_arg(arg_value: str | None, env_name: str) -> str:
    value = (arg_value or os.getenv(env_name) or "").strip()
    if not value:
        raise RepairError(
            f"Missing required DB URL. Pass flag or set env var {env_name}."
        )
    return value


def as_int(value: object, context: str) -> int:
    try:
        return int(value)
    except Exception as exc:
        raise RepairError(f"{context}: expected integer, got {value!r}") from exc


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def sql_ident(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def run_psql(db_url: str, sql_text: str) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["psql", db_url, "-X", "-v", "ON_ERROR_STOP=1", "-q"],
            input=sql_text,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RepairError("psql is required but was not found in PATH.") from exc

    if proc.returncode != 0:
        raise RepairError(
            "psql command failed.\n"
            f"SQL:\n{sql_text}\n\n"
            f"STDERR:\n{proc.stderr.strip()}\n"
            f"STDOUT:\n{proc.stdout.strip()}"
        )
    return proc


def query_csv(db_url: str, query: str) -> List[Dict[str, str]]:
    sql_text = f"copy ({query}) to stdout with csv header"
    proc = run_psql(db_url, sql_text)
    out = proc.stdout
    if not out.strip():
        return []
    reader = csv.DictReader(io.StringIO(out))
    return [dict(row) for row in reader]


def fetch_timeseries_rows(db_url: str, connector_codes: Sequence[str]) -> List[TsRow]:
    where_clause = ""
    if connector_codes:
        code_list = ",".join(sql_literal(code) for code in connector_codes)
        where_clause = f"where c.connector_code in ({code_list})"

    query = f"""
        select
          c.connector_code,
          t.service_ref,
          t.timeseries_ref,
          t.id
        from uk_aq_core.timeseries t
        join uk_aq_core.connectors c on c.id = t.connector_id
        {where_clause}
        order by c.connector_code asc, t.service_ref asc, t.timeseries_ref asc
    """
    rows = query_csv(db_url, query)
    out: List[TsRow] = []
    for row in rows:
        out.append(
            TsRow(
                connector_code=str(row["connector_code"]),
                service_ref=str(row["service_ref"]),
                timeseries_ref=str(row["timeseries_ref"]),
                ts_id=as_int(row["id"], "timeseries.id"),
            )
        )
    return out


def build_map(rows: Iterable[TsRow]) -> Dict[Tuple[str, str, str], int]:
    out: Dict[Tuple[str, str, str], int] = {}
    for row in rows:
        key = (row.connector_code, row.service_ref, row.timeseries_ref)
        existing = out.get(key)
        if existing is not None and existing != row.ts_id:
            raise RepairError(
                "Duplicate natural key with different IDs encountered: "
                f"{key!r} -> {existing} and {row.ts_id}"
            )
        out[key] = row.ts_id
    return out


def print_sample(title: str, rows: Sequence[object], limit: int) -> None:
    print(f"{title}: {len(rows)}")
    for row in rows[: max(limit, 0)]:
        print(f"  {row}")
    if len(rows) > limit >= 0:
        print(f"  ... ({len(rows) - limit} more)")


def discover_fk_timeseries_tables(dest_db_url: str) -> List[TableRef]:
    query = """
        select distinct
          ns.nspname as schema_name,
          cls.relname as table_name
        from pg_constraint con
        join pg_class cls on cls.oid = con.conrelid
        join pg_namespace ns on ns.oid = cls.relnamespace
        join pg_class refcls on refcls.oid = con.confrelid
        join pg_namespace refns on refns.oid = refcls.relnamespace
        where con.contype = 'f'
          and refns.nspname = 'uk_aq_core'
          and refcls.relname = 'timeseries'
          and array_length(con.conkey, 1) = 1
          and array_length(con.confkey, 1) = 1
        order by schema_name asc, table_name asc
    """
    rows = query_csv(dest_db_url, query)
    return [TableRef(schema_name=r["schema_name"], table_name=r["table_name"]) for r in rows]


def discover_timeseries_id_tables(dest_db_url: str) -> List[TableRef]:
    query = """
        select c.table_schema, c.table_name
        from information_schema.columns c
        join information_schema.tables t
          on t.table_schema = c.table_schema
         and t.table_name = c.table_name
        where c.column_name = 'timeseries_id'
          and c.table_schema in ('uk_aq_observs', 'uk_aq_aqilevels', 'uk_aq_ops')
          and t.table_type = 'BASE TABLE'
        group by c.table_schema, c.table_name
        order by c.table_schema asc, c.table_name asc
    """
    rows = query_csv(dest_db_url, query)
    return [TableRef(schema_name=r["table_schema"], table_name=r["table_name"]) for r in rows]


def build_fk_phase_sql(
    fk_tables: Sequence[TableRef],
    from_col: str,
    to_col: str,
) -> str:
    ctes: List[str] = [
        (
            "moved as ("
            "update uk_aq_core.timeseries t "
            f"set id = m.{to_col} "
            "from _uk_aq_ts_id_map m "
            f"where t.id = m.{from_col} "
            f"returning m.{from_col}, m.{to_col}"
            ")"
        )
    ]
    selects: List[str] = ["(select count(*) from moved) as moved_rows"]

    for idx, table in enumerate(fk_tables):
        cte_name = f"u{idx}"
        fq = f"{sql_ident(table.schema_name)}.{sql_ident(table.table_name)}"
        ctes.append(
            (
                f"{cte_name} as ("
                f"update {fq} d "
                f"set timeseries_id = m.{to_col} "
                "from moved m "
                f"where d.timeseries_id = m.{from_col} "
                "returning 1"
                ")"
            )
        )
        alias = f"{table.schema_name}_{table.table_name}_rows"
        selects.append(f"(select count(*) from {cte_name}) as {sql_ident(alias)}")

    return "with " + ", ".join(ctes) + " select " + ", ".join(selects) + ";"


def build_apply_sql(
    *,
    mismatches: Sequence[Mismatch],
    fk_tables: Sequence[TableRef],
    non_fk_tables: Sequence[TableRef],
    lock_timeout_ms: int,
) -> str:
    max_pair = max(max(m.src_id, m.dst_id) for m in mismatches)
    temp_start = max_pair + 1
    if temp_start + len(mismatches) >= 2_147_483_647:
        raise RepairError("Temp ID allocation would exceed int4 range; aborting for safety.")

    values = []
    for idx, m in enumerate(sorted(mismatches, key=lambda x: (x.dst_id, x.src_id))):
        values.append(f"({m.src_id}, {m.dst_id}, {temp_start + idx})")

    locks = [TableRef("uk_aq_core", "timeseries"), *fk_tables, *non_fk_tables]
    unique_locks = sorted({t.fq(): t for t in locks}.values(), key=lambda t: t.fq())

    statements: List[str] = [
        "begin;",
        f"set local lock_timeout = '{max(lock_timeout_ms, 1)}ms';",
    ]

    for table in unique_locks:
        fq = f"{sql_ident(table.schema_name)}.{sql_ident(table.table_name)}"
        statements.append(f"lock table {fq} in share row exclusive mode;")

    statements.append(
        """
        create temporary table _uk_aq_ts_id_map (
          src_id integer not null,
          dst_id integer not null,
          temp_id integer not null,
          primary key (src_id),
          unique (dst_id),
          unique (temp_id)
        ) on commit drop;
        """.strip()
    )
    statements.append(
        "insert into _uk_aq_ts_id_map (src_id, dst_id, temp_id) values\n  "
        + ",\n  ".join(values)
        + ";"
    )

    statements.append("select 'phase1_dst_to_temp';")
    statements.append(build_fk_phase_sql(fk_tables, from_col="dst_id", to_col="temp_id"))

    statements.append("select 'phase2_temp_to_src';")
    statements.append(build_fk_phase_sql(fk_tables, from_col="temp_id", to_col="src_id"))

    for table in non_fk_tables:
        fq = f"{sql_ident(table.schema_name)}.{sql_ident(table.table_name)}"
        alias = table.fq().replace(".", "_")
        statements.append(f"select 'non_fk_update_{alias}';")
        statements.append(
            "with u as ("
            f"update {fq} d "
            "set timeseries_id = m.src_id "
            "from _uk_aq_ts_id_map m "
            "where d.timeseries_id = m.dst_id "
            "returning 1"
            ") "
            "select count(*) as updated_rows from u;"
        )

    statements.append(
        """
        select count(*) as parent_old_or_temp_remaining
        from uk_aq_core.timeseries t
        join _uk_aq_ts_id_map m on t.id = m.dst_id or t.id = m.temp_id;
        """.strip()
    )

    statements.append(
        """
        select setval(
          pg_get_serial_sequence('uk_aq_core.timeseries', 'id'),
          greatest((select coalesce(max(id), 1) from uk_aq_core.timeseries), 1),
          true
        );
        """.strip()
    )

    statements.append("commit;")
    return "\n\n".join(statements)


def main() -> int:
    args = parse_args()
    source_db_url = env_or_arg(args.source_db_url, "SUPABASE_DB_URL")
    dest_db_url = env_or_arg(args.dest_db_url, "OBS_AQIDB_SUPABASE_DB_URL")

    connector_codes = sorted({code.strip() for code in args.connector_code if code.strip()})

    src_rows = fetch_timeseries_rows(source_db_url, connector_codes)
    dst_rows = fetch_timeseries_rows(dest_db_url, connector_codes)

    src_by_key = build_map(src_rows)
    dst_by_key = build_map(dst_rows)

    all_keys = sorted(set(src_by_key) | set(dst_by_key))

    mismatches: List[Mismatch] = []
    missing_in_dst: List[Tuple[str, str, str, int]] = []
    extra_in_dst: List[Tuple[str, str, str, int]] = []

    for key in all_keys:
        src_id = src_by_key.get(key)
        dst_id = dst_by_key.get(key)
        if src_id is None and dst_id is not None:
            extra_in_dst.append((key[0], key[1], key[2], dst_id))
        elif dst_id is None and src_id is not None:
            missing_in_dst.append((key[0], key[1], key[2], src_id))
        elif src_id is not None and dst_id is not None and src_id != dst_id:
            mismatches.append(
                Mismatch(
                    connector_code=key[0],
                    service_ref=key[1],
                    timeseries_ref=key[2],
                    src_id=src_id,
                    dst_id=dst_id,
                )
            )

    print("Timeseries alignment summary:")
    print(f"  connector_filter={connector_codes if connector_codes else 'ALL'}")
    print(f"  source_rows={len(src_rows)}")
    print(f"  destination_rows={len(dst_rows)}")
    print(f"  id_mismatches={len(mismatches)}")
    print(f"  missing_in_destination={len(missing_in_dst)}")
    print(f"  extra_in_destination={len(extra_in_dst)}")

    if mismatches:
        print_sample(
            "Sample mismatches (connector_code, service_ref, timeseries_ref, src_id, dst_id)",
            [
                (
                    m.connector_code,
                    m.service_ref,
                    m.timeseries_ref,
                    m.src_id,
                    m.dst_id,
                )
                for m in mismatches
            ],
            args.report_limit,
        )
    if missing_in_dst:
        print_sample(
            "Sample missing_in_destination (connector_code, service_ref, timeseries_ref, src_id)",
            missing_in_dst,
            args.report_limit,
        )
    if extra_in_dst:
        print_sample(
            "Sample extra_in_destination (connector_code, service_ref, timeseries_ref, dst_id)",
            extra_in_dst,
            args.report_limit,
        )

    if not mismatches:
        print("No ID mismatches found. Nothing to repair.")
        return 0

    src_ids = [m.src_id for m in mismatches]
    dst_ids = [m.dst_id for m in mismatches]
    if len(src_ids) != len(set(src_ids)):
        raise RepairError("Unsafe mapping: duplicate src_id detected in mismatch set.")
    if len(dst_ids) != len(set(dst_ids)):
        raise RepairError("Unsafe mapping: duplicate dst_id detected in mismatch set.")

    dest_id_to_key = {
        row.ts_id: (row.connector_code, row.service_ref, row.timeseries_ref) for row in dst_rows
    }
    blocking = [
        (src_id, *dest_id_to_key[src_id])
        for src_id in src_ids
        if src_id in dest_id_to_key and src_id not in set(dst_ids)
    ]
    if blocking:
        print_sample(
            "Blocking destination occupants (src_id already used outside mismatch dst IDs)",
            blocking,
            args.report_limit,
        )
        raise RepairError("Cannot apply repair because canonical src IDs are occupied by other destination rows.")

    fk_tables = discover_fk_timeseries_tables(dest_db_url)
    all_tsid_tables = discover_timeseries_id_tables(dest_db_url)
    fk_set = {t.fq() for t in fk_tables}
    non_fk_tables = [t for t in all_tsid_tables if t.fq() not in fk_set]

    print("Dependent tables:")
    print("  FK tables referencing uk_aq_core.timeseries(id):")
    for table in fk_tables:
        print(f"    {table.fq()}")
    print("  Additional timeseries_id tables (non-FK):")
    for table in non_fk_tables:
        print(f"    {table.fq()}")

    if (missing_in_dst or extra_in_dst) and not args.allow_key_delta:
        print("\nApply is blocked because natural-key sets differ between source and destination.")
        print(
            "Run core mirror sync first or re-run with --allow-key-delta if you intentionally want partial ID repair."
        )
        if args.apply:
            return 2

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to execute repair.")
        return 0

    apply_sql = build_apply_sql(
        mismatches=mismatches,
        fk_tables=fk_tables,
        non_fk_tables=non_fk_tables,
        lock_timeout_ms=args.lock_timeout_ms,
    )
    apply_proc = run_psql(dest_db_url, apply_sql)
    if apply_proc.stdout.strip():
        print("Apply output:")
        print(apply_proc.stdout.strip())

    # Post-apply validation
    src_after = build_map(fetch_timeseries_rows(source_db_url, connector_codes))
    dst_after = build_map(fetch_timeseries_rows(dest_db_url, connector_codes))

    residual = 0
    for key in set(src_after) & set(dst_after):
        if src_after[key] != dst_after[key]:
            residual += 1

    if residual != 0:
        raise RepairError(f"Post-apply validation failed: {residual} ID mismatches remain.")

    print("Post-apply validation passed: destination IDs now match source IDs for shared natural keys.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepairError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
