"""Shared client contract for the central phenomena mapping RPC."""

from typing import Any, Dict, Iterable, List


RPC_NAME = "uk_aq_rpc_phenomena_upsert"


def normalize_phenomena_payload(
    rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    payload = [dict(row) for row in rows if row and row.get("source_label")]
    source_labels = [str(row["source_label"]) for row in payload]
    if len(source_labels) != len(set(source_labels)):
        raise RuntimeError("Duplicate source_label values in phenomena payload.")
    return payload


def validate_phenomena_results(
    payload: List[Dict[str, Any]],
    result_rows: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    results = {
        str(row.get("source_label")): dict(row)
        for row in result_rows
        if row and row.get("source_label") and row.get("phenomenon_id") is not None
    }
    missing = sorted(
        str(row["source_label"])
        for row in payload
        if str(row["source_label"]) not in results
    )
    if missing:
        raise RuntimeError(
            "Central phenomena RPC did not return IDs for: " + ", ".join(missing)
        )
    warnings = sorted(
        f"{source_label}:{row['mapping_warning']}"
        for source_label, row in results.items()
        if row.get("mapping_warning")
    )
    if warnings:
        raise RuntimeError(
            "Central phenomena RPC returned unmapped source warnings: "
            + ", ".join(warnings)
        )
    return results


def upsert_phenomena_via_rpc(
    public_client: Any,
    rows: Iterable[Dict[str, Any]],
    *,
    allow_mapping_upsert: bool = False,
) -> Dict[str, Dict[str, Any]]:
    payload = normalize_phenomena_payload(rows)
    if not payload:
        return {}
    params: Dict[str, Any] = {"rows": payload}
    if allow_mapping_upsert:
        params["p_allow_mapping_upsert"] = True
    response = public_client.rpc(RPC_NAME, params).execute()
    result_rows = response.data if hasattr(response, "data") else response.get("data")
    return validate_phenomena_results(payload, result_rows or [])
