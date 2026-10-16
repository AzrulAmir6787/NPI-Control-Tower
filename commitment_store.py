
from datetime import date
from typing import Optional

import streamlit as st
from supabase import Client, create_client


def _get_client() -> Client:
    """Create a Supabase client using local or deployed Streamlit secrets."""
    config = st.secrets["supabase"]

    return create_client(
        config["url"],
        config["key"],
    )


def get_commitment(part_key: str) -> Optional[dict]:
    """Return the Latest commitment for one part, or None if not found."""
    key = str(part_key).strip()

    if not key:
        raise ValueError("part_key cannot be empty")

    client = _get_client()

    response = (
        client.table("commitments")
        .select(
            "part_key, owner, commitment_date, "
            "completion_date, remarks, updated_by, "
            "updated_at, version"
        )
        .eq("part_key", key)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def get_previous_commitment(part_key: str) -> Optional[dict]:
    """Return the Previous commitment for one part, if available."""
    key = str(part_key).strip()

    if not key:
        raise ValueError("part_key cannot be empty")

    client = _get_client()

    response = (
        client.table("commitments_previous")
        .select(
            "part_key, owner, commitment_date, "
            "completion_date, remarks, updated_by, "
            "updated_at, version"
        )
        .eq("part_key", key)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def save_commitment(
    part_key: str,
    owner: Optional[str],
    commitment_date: Optional[date],
    completion_date: Optional[date],
    remarks: Optional[str],
    updated_by: Optional[str] = None,
) -> dict:
    """
    Save a complete set of current field values.

    The database function moves Latest to Previous and writes
    the new Latest in one transaction.
    """
    key = str(part_key).strip()

    if not key:
        raise ValueError("part_key cannot be empty")

    client = _get_client()

    response = client.rpc(
        "save_commitment",
        {
            "p_part_key": key,
            "p_owner": owner or None,
            "p_commitment_date": (
                commitment_date.isoformat()
                if commitment_date
                else None
            ),
            "p_completion_date": (
                completion_date.isoformat()
                if completion_date
                else None
            ),
            "p_remarks": remarks or "",
            "p_updated_by": updated_by or None,
        },
    ).execute()

    return response.data


def save_commitment_patch(
    part_key: str,
    changes: dict,
    updated_by: Optional[str] = None,
) -> dict:
    """
    Save only changed fields while preserving all other current values.

    Allowed keys:
      owner
      commitment_date
      completion_date
      remarks
    """
    key = str(part_key).strip()

    if not key:
        raise ValueError("part_key cannot be empty")

    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes must be a non-empty dictionary")

    allowed_fields = {
        "owner",
        "commitment_date",
        "completion_date",
        "remarks",
    }

    unknown_fields = set(changes) - allowed_fields

    if unknown_fields:
        raise ValueError(
            f"Unsupported commitment fields: {sorted(unknown_fields)}"
        )

    payload = {}

    for field, value in changes.items():
        if field in {"commitment_date", "completion_date"}:
            payload[field] = (
                value.isoformat()
                if isinstance(value, date)
                else value
            )
        elif field == "owner":
            payload[field] = value or None
        elif field == "remarks":
            payload[field] = value or ""

    client = _get_client()

    response = client.rpc(
        "save_commitment_patch",
        {
            "p_part_key": key,
            "p_changes": payload,
            "p_updated_by": updated_by or None,
        },
    ).execute()

    return response.data


def get_commitments(part_keys: list[str]) -> dict[str, dict]:
    """
    Fetch Latest commitments for multiple part keys.

    Returns:
        {
            "PART-001": {
                "part_key": "PART-001",
                "owner": "...",
                ...
            }
        }
    """
    keys = list(dict.fromkeys(
        str(key).strip()
        for key in part_keys
        if key is not None and str(key).strip()
    ))

    if not keys:
        return {}

    client = _get_client()
    results = {}

    # Use batches to avoid oversized requests.
    batch_size = 200

    for start in range(0, len(keys), batch_size):
        batch = keys[start:start + batch_size]

        response = (
            client.table("commitments")
            .select(
                "part_key, owner, commitment_date, "
                "completion_date, remarks, updated_by, "
                "updated_at, version"
            )
            .in_("part_key", batch)
            .execute()
        )

        for record in response.data or []:
            results[record["part_key"]] = record

    return results