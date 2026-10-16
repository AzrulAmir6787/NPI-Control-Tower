from datetime import date, datetime
from typing import Optional

import streamlit as st
from supabase import Client, create_client


def _get_client(user_id: str) -> Client:
    """Create service-role client only after rechecking the caller is APPROVED."""
    identity = str(user_id or "").strip()
    if not identity:
        raise PermissionError("A verified signed-in user is required.")

    config = st.secrets["supabase"]
    url = str(config.get("url") or config.get("SUPABASE_URL") or "").strip()
    secret_key = ""
    for name in ("secret_key", "service_role_key", "service_key", "SUPABASE_SECRET_KEY", "key"):
        if config.get(name):
            secret_key = str(config[name]).strip()
            break
    if not url or not secret_key:
        raise RuntimeError("Supabase URL or server secret key is not configured.")

    client = create_client(url, secret_key)
    response = (
        client.table("user_access")
        .select("user_id, status")
        .eq("user_id", identity)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows or rows[0].get("status") != "APPROVED":
        raise PermissionError("Only APPROVED users may read or change commitments.")
    return client


def _clean_key(part_key: str) -> str:
    key = str(part_key or "").strip()
    if not key:
        raise ValueError("part_key cannot be empty")
    return key


def _record_columns():
    return "part_key, owner, commitment_date, completion_date, remarks, updated_by, updated_at, version"


def get_commitment(part_key: str, *, user_id: str) -> Optional[dict]:
    key = _clean_key(part_key)
    response = (
        _get_client(user_id).table("commitments")
        .select(_record_columns()).eq("part_key", key).limit(1).execute()
    )
    return response.data[0] if response.data else None


def get_previous_commitment(part_key: str, *, user_id: str) -> Optional[dict]:
    key = _clean_key(part_key)
    response = (
        _get_client(user_id).table("commitments_previous")
        .select(_record_columns()).eq("part_key", key).limit(1).execute()
    )
    return response.data[0] if response.data else None


def _date_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    text_value = str(value).strip()
    return text_value or None


def save_commitment(part_key: str, owner: Optional[str], commitment_date, completion_date, remarks: Optional[str], *, user_id: str) -> dict:
    key = _clean_key(part_key)
    client = _get_client(user_id)
    response = client.rpc("save_commitment", {
        "p_part_key": key,
        "p_owner": str(owner or "").strip() or None,
        "p_commitment_date": _date_value(commitment_date),
        "p_completion_date": _date_value(completion_date),
        "p_remarks": str(remarks or ""),
        "p_updated_by": str(user_id),
    }).execute()
    return response.data


def save_commitment_patch(part_key: str, changes: dict, *, user_id: str) -> dict:
    key = _clean_key(part_key)
    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes must be a non-empty dictionary")
    allowed = {"owner", "commitment_date", "completion_date", "remarks"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported commitment fields: {sorted(unknown)}")
    payload = {}
    for field, value in changes.items():
        if field in {"commitment_date", "completion_date"}:
            payload[field] = _date_value(value)
        elif field == "owner":
            payload[field] = str(value or "").strip() or None
        else:
            payload[field] = str(value or "")
    response = _get_client(user_id).rpc("save_commitment_patch", {
        "p_part_key": key,
        "p_changes": payload,
        "p_updated_by": str(user_id),
    }).execute()
    return response.data


def get_commitments(part_keys: list[str], *, user_id: str) -> dict[str, dict]:
    keys = list(dict.fromkeys(str(key).strip() for key in part_keys if key is not None and str(key).strip()))
    if not keys:
        # Still validate access so an empty query cannot bypass the access gate.
        _get_client(user_id)
        return {}
    client = _get_client(user_id)
    results = {}
    for start in range(0, len(keys), 200):
        response = (
            client.table("commitments").select(_record_columns())
            .in_("part_key", keys[start:start + 200]).execute()
        )
        for record in response.data or []:
            results[record["part_key"]] = record
    return results
