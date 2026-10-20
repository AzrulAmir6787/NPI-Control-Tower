import base64
import hashlib
import hmac
import html
import io
import json
import math
import os
import secrets as py_secrets
import threading
import time

import extra_streamlit_components as stx
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from data_loader import (
    get_sheet_names,
    load_sheet_with_pictures,
    get_picture_for_part,
    get_picture_summary,
)

from calculations import (
    build_calculation_layer,
    build_unique_part_dataset,
    add_aging_fields,
    build_production_hotspot,
)

from actions import (
    create_empty_action_store,
    calculate_commitment_status,
)

from commitment_store_secure import (
    get_commitments as get_supabase_commitments,
    save_commitment_patch as save_supabase_commitment_patch,
)

_npi_drilldown_component = components.declare_component(
    "npi_drilldown_commitment_bridge",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "npi_drilldown_component"),
)


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="NPI Control Tower",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# SUPABASE AUTH / PERSISTENT SESSION ACCESS GATE
# ============================================================

from supabase import create_client


def _secret_value(config, *names):
    for name in names:
        value = config.get(name)
        if value:
            return str(value)
    return ""


try:
    _supabase_config = st.secrets["supabase"]
    SUPABASE_URL = _secret_value(_supabase_config, "url", "SUPABASE_URL")
    SUPABASE_PUBLISHABLE_KEY = _secret_value(
        _supabase_config, "publishable_key", "anon_key", "SUPABASE_PUBLISHABLE_KEY"
    )
    SUPABASE_SECRET_KEY = _secret_value(
        _supabase_config, "secret_key", "service_role_key", "service_key", "SUPABASE_SECRET_KEY"
    )
except (FileNotFoundError, KeyError, TypeError):
    SUPABASE_URL = ""
    SUPABASE_PUBLISHABLE_KEY = ""
    SUPABASE_SECRET_KEY = ""

if not (SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY and SUPABASE_SECRET_KEY):
    st.error(
        "Supabase configuration is incomplete. Please check the [supabase] section "
        "in .streamlit/secrets.toml. Required values: url, publishable_key, secret_key."
    )
    st.stop()


def _new_auth_client():
    # Use the publishable key for user sign-up/sign-in. Do not share this client
    # between users because its Auth session is mutable.
    return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


def _new_admin_client():
    # Server-side only. The secret key must never be sent to the browser.
    return create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


def _get_user_access(user_id):
    response = (
        _new_admin_client()
        .table("user_access")
        .select("user_id, full_name, status, role")
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    return response.data


# Streamlit Session State is tied to its WebSocket connection. Keep the existing
# per-browser state preservation, but the browser cookie is now only an opaque,
# signed session ID. Supabase tokens and dashboard state stay server-side.
SESSION_COOKIE_NAME = "npi_control_tower_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days, or until explicit logout
PERSISTED_STATE_KEYS = {
    "raw_df",
    "calc_df",
    "unique_df",
    "picture_map",
    "sheet_name",
    "selected_classification",
    "selected_customer",
    "actions_df",
    "drilldown_jump_id",
    "selected_delay_bucket",
    "selected_hotspot",
    "selected_commitment_status",
    "control_tower_customer",
    "module_selector",
    "sheet_selector",
    "drilldown_current_process_filter",
    "selected_enquiry_parts",
    "enquiry_search_active",
    "enquiry_search_message",
    "enquiry_missing_parts",
    "show_enquiry_missing_dialog",
    "enquiry_last_query",
    "enquiry_input_text",
    "enquiry_last_input_count",
    "enquiry_last_matched_count",
}


@st.cache_resource
def _get_persistent_session_store():
    """Shared in-memory cache, keyed by an unguessable per-browser token."""
    return {"sessions": {}, "revoked": {}, "lock": threading.RLock()}


_persistent_store = _get_persistent_session_store()


def _make_session_cookie(user_id):
    issued_at = str(int(time.time()))
    nonce = py_secrets.token_urlsafe(32)
    payload = f"npi-control-tower-v2|{user_id}|{issued_at}|{nonce}"
    signature = hmac.new(
        SUPABASE_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{issued_at}.{nonce}.{signature}"


def _verify_session_cookie(token):
    if not isinstance(token, str) or not token:
        return False
    try:
        issued_at_text, nonce, supplied_signature = token.split(".", 2)
        issued_at = int(issued_at_text)
    except (TypeError, ValueError):
        return False

    now = time.time()
    age = now - issued_at
    if age < -60 or age > SESSION_TTL_SECONDS:
        return False

    with _persistent_store["lock"]:
        for old_token, expires_at in list(_persistent_store["revoked"].items()):
            if expires_at <= now:
                _persistent_store["revoked"].pop(old_token, None)
        if _persistent_store["revoked"].get(token, 0) > now:
            return False
        record = _persistent_store["sessions"].get(token)
        # A signed cookie alone is not enough: its server-side login record must exist.
        if not record or not record.get("user_id"):
            return False
        user_id = record["user_id"]

    payload = f"npi-control-tower-v2|{user_id}|{issued_at_text}|{nonce}"
    expected_signature = hmac.new(
        SUPABASE_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def _get_or_create_session_record(token):
    now = time.time()
    with _persistent_store["lock"]:
        for old_token, record in list(_persistent_store["sessions"].items()):
            if record.get("expires_at", 0) <= now:
                _persistent_store["sessions"].pop(old_token, None)
        record = _persistent_store["sessions"].get(token)
        return record, record is None


def _capture_persistent_state():
    state = {}
    for key in PERSISTED_STATE_KEYS:
        if key in st.session_state:
            state[key] = st.session_state[key]
    for key in list(st.session_state.keys()):
        if str(key).startswith("drilldown_page_state_"):
            state[key] = st.session_state[key]
    return state


def _save_persistent_state():
    token = st.session_state.get("_npi_session_token")
    if not token or not st.session_state.get("npi_authenticated", False):
        return
    with _persistent_store["lock"]:
        record = _persistent_store["sessions"].get(token)
        if record is not None:
            record["state"] = _capture_persistent_state()


def _restore_persistent_state(record):
    state = record.get("state", {})
    for key, value in state.items():
        if key in PERSISTED_STATE_KEYS or str(key).startswith("drilldown_page_state_"):
            st.session_state[key] = value


cookie_manager = stx.CookieManager(key="npi_control_tower_cookie_manager")
try:
    browser_cookies = cookie_manager.get_all()
except Exception:
    browser_cookies = {}
if not isinstance(browser_cookies, dict):
    browser_cookies = {}

session_token = (
    st.session_state.get("_npi_session_token")
    or browser_cookies.get(SESSION_COOKIE_NAME)
)

if _verify_session_cookie(session_token):
    session_record, session_record_created = _get_or_create_session_record(session_token)
    if session_record is not None:
        st.session_state["_npi_session_token"] = session_token
        st.session_state["npi_authenticated"] = True
        st.session_state["npi_user_id"] = session_record.get("user_id")
        st.session_state["npi_user_full_name"] = session_record.get("full_name", "")
        st.session_state["npi_user_role"] = session_record.get("role", "USER")
        if not st.session_state.get("_npi_persistent_state_loaded", False):
            _restore_persistent_state(session_record)
            st.session_state["_npi_persistent_state_loaded"] = True
            if session_record_created:
                st.session_state["_npi_state_recovery_notice"] = True
else:
    st.session_state["npi_authenticated"] = False
    st.session_state.pop("_npi_session_token", None)
    st.session_state.pop("_npi_persistent_state_loaded", None)


def _delete_login_cookie():
    try:
        cookie_manager.delete(
            SESSION_COOKIE_NAME, key="delete_npi_control_tower_session"
        )
    except Exception:
        pass


def _logout_current_session():
    token = st.session_state.get("_npi_session_token")
    if token:
        try:
            issued_at = int(token.split(".", 1)[0])
            token_expiry = issued_at + SESSION_TTL_SECONDS
        except (TypeError, ValueError):
            token_expiry = time.time() + SESSION_TTL_SECONDS
        with _persistent_store["lock"]:
            record = _persistent_store["sessions"].pop(token, None)
            _persistent_store["revoked"][token] = token_expiry
        if record and record.get("access_token") and record.get("refresh_token"):
            try:
                auth_client = _new_auth_client()
                auth_client.auth.set_session(
                    record["access_token"], record["refresh_token"]
                )
                auth_client.auth.sign_out()
            except Exception:
                pass

    _delete_login_cookie()
    for key in list(st.session_state.keys()):
        if (
            key in PERSISTED_STATE_KEYS
            or str(key).startswith("drilldown_page_state_")
            or key in {
                "baq_upload", "_npi_session_token", "_npi_persistent_state_loaded",
                "_npi_state_recovery_notice", "npi_user_id", "npi_user_full_name",
                "npi_user_role",
            }
        ):
            try:
                del st.session_state[key]
            except Exception:
                pass
    st.session_state["npi_authenticated"] = False
    st.rerun()


# Check authorization on the server on every app rerun, not only when logging in.
_access_notice = st.session_state.pop("_npi_access_notice", None)
if st.session_state.get("npi_authenticated", False):
    try:
        _current_access = _get_user_access(st.session_state.get("npi_user_id"))
    except Exception:
        st.error("Unable to verify your account access. Please refresh or try again later.")
        st.stop()

    if not _current_access or _current_access.get("status") != "APPROVED":
        _status = (_current_access or {}).get("status", "NO PROFILE")
        token = st.session_state.get("_npi_session_token")
        if token:
            with _persistent_store["lock"]:
                _persistent_store["sessions"].pop(token, None)
        _delete_login_cookie()
        st.session_state["npi_authenticated"] = False
        st.session_state.pop("_npi_session_token", None)
        st.session_state.pop("_npi_persistent_state_loaded", None)
        st.session_state.pop("npi_user_id", None)
        st.session_state.pop("npi_user_full_name", None)
        st.session_state.pop("npi_user_role", None)
        _access_notice = (
            "Your account is pending Admin approval."
            if _status == "PENDING"
            else "Your account does not currently have dashboard access. Please contact the Admin."
        )
    else:
        st.session_state["npi_user_full_name"] = _current_access.get("full_name", "")
        st.session_state["npi_user_role"] = _current_access.get("role", "USER")


if not st.session_state.get("npi_authenticated", False):
    st.title("NPI Control Tower")
    st.caption("Sign in with your individual account, or create an account for Admin approval.")
    if _access_notice:
        st.warning(_access_notice)

    auth_mode = st.radio(
        "Account",
        ["Log in", "Sign up"],
        horizontal=True,
        key="npi_auth_mode",
    )

    if auth_mode == "Sign up":
        with st.form("npi_signup_form"):
            full_name = st.text_input("Full Name")
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            confirm_password = st.text_input("Confirm Password", type="password")
            signup_submitted = st.form_submit_button("Create account")

        if signup_submitted:
            full_name = full_name.strip()
            email = email.strip()
            if not full_name or not email or not password or not confirm_password:
                st.error("Please complete all fields.")
            elif password != confirm_password:
                st.error("Password and Confirm Password do not match.")
            else:
                try:
                    signup_client = _new_auth_client()
                    signup_response = signup_client.auth.sign_up(
                        {
                            "email": email,
                            "password": password,
                            "options": {"data": {"full_name": full_name}},
                        }
                    )
                    signup_user = getattr(signup_response, "user", None)
                    if signup_user is None:
                        st.error("Sign-up did not return a user account. Please check the details and try again.")
                    else:
                        signup_access = _get_user_access(signup_user.id)
                        if signup_access and signup_access.get("status") == "PENDING":
                            st.success("Account created. Your status is PENDING; an Admin must approve it before you can access the dashboard.")
                        elif signup_access:
                            st.success("Account created. Please use Log in to continue once your access is approved.")
                        else:
                            st.error("The Auth account was created, but its user_access profile was not found. Please contact the Admin before trying again.")
                except Exception as exc:
                    message = str(exc)
                    if "already registered" in message.lower() or "already exists" in message.lower():
                        st.error("An account with this email may already exist. Try Log in or contact the Admin.")
                    else:
                        st.error(f"Sign-up failed: {message}")

    else:
        with st.form("npi_login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            login_submitted = st.form_submit_button("Log in")

        if login_submitted:
            if not email.strip() or not password:
                st.error("Please enter your email and password.")
            else:
                try:
                    auth_client = _new_auth_client()
                    auth_response = auth_client.auth.sign_in_with_password(
                        {"email": email.strip(), "password": password}
                    )
                    auth_user = getattr(auth_response, "user", None)
                    auth_session = getattr(auth_response, "session", None)
                    if auth_user is None or auth_session is None:
                        st.error("Login did not return a valid session. Please try again.")
                    else:
                        user_access = _get_user_access(auth_user.id)
                        if not user_access:
                            st.error("Your account profile was not found. Please contact the Admin.")
                        elif user_access.get("status") == "PENDING":
                            st.warning("Your account is pending Admin approval. You can log in after it is approved.")
                        elif user_access.get("status") == "SUSPENDED":
                            st.error("Your account is suspended. Please contact the Admin.")
                        elif user_access.get("status") != "APPROVED":
                            st.error("Your account does not currently have dashboard access. Please contact the Admin.")
                        else:
                            new_token = _make_session_cookie(auth_user.id)
                            expiry = int(new_token.split(".", 1)[0]) + SESSION_TTL_SECONDS
                            with _persistent_store["lock"]:
                                _persistent_store["sessions"][new_token] = {
                                    "expires_at": expiry,
                                    "state": {},
                                    "user_id": str(auth_user.id),
                                    "access_token": auth_session.access_token,
                                    "refresh_token": auth_session.refresh_token,
                                    "full_name": user_access.get("full_name", ""),
                                    "role": user_access.get("role", "USER"),
                                }

                            try:
                                try:
                                    request_host = str(st.context.headers.get("Host", "")).lower()
                                except Exception:
                                    request_host = ""
                                secure_cookie = not (
                                    request_host.startswith("localhost")
                                    or request_host.startswith("127.0.0.1")
                                )
                                cookie_manager.set(
                                    cookie=SESSION_COOKIE_NAME,
                                    val=new_token,
                                    key="set_npi_control_tower_session",
                                    max_age=SESSION_TTL_SECONDS,
                                    path="/",
                                    secure=secure_cookie,
                                    same_site="lax",
                                )
                            except Exception:
                                with _persistent_store["lock"]:
                                    _persistent_store["sessions"].pop(new_token, None)
                                st.error("Unable to create a persistent login cookie. Please refresh and try again.")
                                st.stop()

                            st.session_state["_npi_session_token"] = new_token
                            st.session_state["npi_authenticated"] = True
                            st.session_state["npi_user_id"] = str(auth_user.id)
                            st.session_state["npi_user_full_name"] = user_access.get("full_name", "")
                            st.session_state["npi_user_role"] = user_access.get("role", "USER")
                            st.session_state["_npi_persistent_state_loaded"] = False
                            st.rerun()
                except Exception as exc:
                    message = str(exc).lower()
                    if "invalid login credentials" in message or "invalid credentials" in message:
                        st.error("Invalid email or password.")
                    else:
                        st.error("Unable to log in. Please check your details or try again later.")

    st.stop()


with st.sidebar:
    st.caption(f"Signed in: {st.session_state.get('npi_user_full_name', '')}")
    if st.button("Log out", key="npi_logout_button", use_container_width=True):
        _logout_current_session()


# ============================================================
# SESSION STATE
# ============================================================

DEFAULT_STATE = {
    "raw_df": None,
    "calc_df": None,
    "unique_df": None,
    "picture_map": {},
    "sheet_name": None,
    "selected_classification": "ALL",
    "selected_customer": "ALL",
    "actions_df": create_empty_action_store(),

    # Every Executive Status click increments this counter.
    # The JavaScript controller detects a NEW number and
    # performs a new jump.
    "drilldown_jump_id": 0,
    "selected_delay_bucket": "",
    "selected_hotspot": "",
    "selected_commitment_status": "",
    "drilldown_current_process_filter": "ALL",
    "selected_enquiry_parts": [],
    "enquiry_search_active": False,
    "enquiry_search_message": "",
    "enquiry_missing_parts": [],
    "show_enquiry_missing_dialog": False,
    "enquiry_last_query": "",
    "enquiry_input_text": "",
    "enquiry_last_input_count": 0,
    "enquiry_last_matched_count": 0,
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# UPLOADED FILE ADAPTER
# ============================================================

class UploadedFileAdapter(io.BytesIO):
    """
    Gives data_loader.py a normal file-like object while
    preserving the original filename and extension.
    """

    def __init__(self, uploaded_file):
        self.original_name = uploaded_file.name
        self.name = uploaded_file.name
        self.filename = uploaded_file.name
        self.file_name = uploaded_file.name

        super().__init__(
            uploaded_file.getvalue()
        )


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_text(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    return str(value)


def html_escape(value):
    return html.escape(
        safe_text(value)
    )


def fmt_number(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def fmt_date(value):
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""

        return pd.to_datetime(
            value
        ).strftime("%d/%m/%Y")

    except Exception:
        return safe_text(value)


def image_to_base64(image_bytes):
    if not image_bytes:
        return None

    try:
        return base64.b64encode(
            image_bytes
        ).decode("utf-8")
    except Exception:
        return None


def first_existing_column(
    df,
    candidates,
):
    for col in candidates:
        if col in df.columns:
            return col

    return None


def normalize_part_number(value):
    """Normalize part numbers for whitespace-insensitive, case-insensitive exact matching."""
    return safe_text(value).strip().casefold()


def build_delay_commitment_status_map(unique_df, actions_df, control_date):
    """Map each delayed unique part in the current customer scope to its commitment status.

    The unique-part dataset remains the source of truth. A delayed part without an
    action record or commitment date is classified as NO COMMITMENT. Completed
    actions are excluded from the four open-commitment tiles.
    """
    status_map = {}
    if not isinstance(unique_df, pd.DataFrame) or unique_df.empty:
        return status_map

    part_col = first_existing_column(
        unique_df,
        ["PART NUMBER", "Part Number", "Subpart Part Num", "PART", "Part"],
    )
    if not part_col:
        return status_map

    work_df = unique_df.copy()
    class_col = first_existing_column(
        work_df,
        ["CALC_CLASSIFICATION", "CLASSIFICATION"],
    )
    if class_col:
        work_df = work_df[
            work_df[class_col].fillna("").astype(str).str.strip().str.upper().isin(
                {"RED - ENGINEERING", "RED - PROCESS", "RED - PRODUCTION"}
            )
        ].copy()

    action_status_by_part = {}
    if isinstance(actions_df, pd.DataFrame) and not actions_df.empty:
        action_part_col = first_existing_column(
            actions_df,
            ["PART_NUMBER", "Part Number", "PART NUMBER", "Subpart Part Num"],
        )
        commitment_date_col = first_existing_column(
            actions_df,
            ["COMMITMENT_DATE", "Commitment Date"],
        )
        action_status_col = first_existing_column(
            actions_df,
            ["ACTION_STATUS", "Action Status", "STATUS", "Commitment Status"],
        )
        if action_part_col:
            for _, action_row in actions_df.iterrows():
                part_key = normalize_part_number(action_row.get(action_part_col, ""))
                if not part_key:
                    continue
                status = calculate_commitment_status(
                    action_row.get(commitment_date_col, "") if commitment_date_col else "",
                    action_row.get(action_status_col, "") if action_status_col else "",
                    control_date,
                )
                # Keep the last stored record for a part, matching the existing drilldown/export lookup.
                action_status_by_part[part_key] = status

    allowed_statuses = {"OVERDUE", "DUE TODAY", "UPCOMING", "NO COMMITMENT"}
    for part_value in work_df[part_col].tolist():
        part_key = normalize_part_number(part_value)
        if not part_key:
            continue
        status = action_status_by_part.get(part_key, "NO COMMITMENT")
        if status in allowed_statuses:
            status_map[part_key] = status

    return status_map


def _run_enquiry_search():
    """Search the uploaded unique-part dataset from a native Streamlit form."""
    raw_payload = safe_text(
        st.session_state.get("enquiry_input_text", "")
    )
    requested = []
    seen = set()

    for line in raw_payload.splitlines():
        item = line.strip()
        if not item:
            continue
        normalized = normalize_part_number(item)
        if normalized and normalized not in seen:
            requested.append(item)
            seen.add(normalized)

    st.session_state["enquiry_last_query"] = "\n".join(requested)
    st.session_state["enquiry_last_input_count"] = len(requested)
    st.session_state["enquiry_missing_parts"] = []
    st.session_state["show_enquiry_missing_dialog"] = False

    if not requested:
        st.session_state["selected_enquiry_parts"] = []
        st.session_state["enquiry_search_active"] = False
        st.session_state["enquiry_last_matched_count"] = 0
        st.session_state["enquiry_search_message"] = (
            "Enter one full part number per line, then click SEARCH."
        )
        return

    source_df = st.session_state.get("unique_df")
    part_col = first_existing_column(
        source_df,
        ["PART NUMBER", "Part Number", "Subpart Part Num", "PART", "Part"],
    ) if isinstance(source_df, pd.DataFrame) else None

    if source_df is None or source_df.empty or part_col is None:
        matched_keys = []
        missing_parts = requested[:]
    else:
        available = {}
        for value in source_df[part_col].tolist():
            normalized = normalize_part_number(value)
            if normalized and normalized not in available:
                available[normalized] = safe_text(value).strip()

        matched_keys = []
        missing_parts = []
        for item in requested:
            normalized = normalize_part_number(item)
            if normalized in available:
                if normalized not in matched_keys:
                    matched_keys.append(normalized)
            else:
                missing_parts.append(item)

    st.session_state["selected_enquiry_parts"] = matched_keys
    st.session_state["enquiry_search_active"] = True
    st.session_state["enquiry_missing_parts"] = missing_parts
    st.session_state["enquiry_last_matched_count"] = len(matched_keys)
    st.session_state["enquiry_search_message"] = (
        f"Matched {len(matched_keys):,} of {len(requested):,} part number(s). "
        f"Not found: {len(missing_parts):,}."
    )
    st.session_state["show_enquiry_missing_dialog"] = bool(missing_parts)

    # Enquiry searches span the full uploaded BAQ, not only the previously
    # selected customer/classification/bucket/hotspot.
    st.session_state["selected_customer"] = "ALL"
    if "control_tower_customer" in st.session_state:
        st.session_state["control_tower_customer"] = "ALL"
    st.session_state["selected_classification"] = "ALL"
    st.session_state["selected_delay_bucket"] = ""
    st.session_state["selected_hotspot"] = ""
    st.session_state["selected_commitment_status"] = ""
    st.session_state["drilldown_current_process_filter"] = "ALL"
    for page_key in list(st.session_state.keys()):
        if str(page_key).startswith("drilldown_page_state_"):
            st.session_state[page_key] = 1


def _dismiss_enquiry_missing_dialog():
    st.session_state["show_enquiry_missing_dialog"] = False


@st.dialog("Part number(s) not found", width="small")
def _render_enquiry_missing_dialog():
    missing_parts = st.session_state.get("enquiry_missing_parts", [])
    st.warning(
        f"{len(missing_parts):,} part number(s) did not match the uploaded BAQ."
    )
    st.caption("These part numbers were not found:")
    st.code("\n".join(str(part) for part in missing_parts), language=None)

    # A dialog runs as a Streamlit fragment. Updating only the flag in a
    # button callback can leave the dialog open; explicitly rerun the full app
    # so the top-level conditional stops rendering this dialog.
    if st.button(
        "Close",
        key="enquiry_missing_dialog_close",
        use_container_width=True,
    ):
        st.session_state["show_enquiry_missing_dialog"] = False
        st.rerun(scope="app")


# ============================================================
# DATA BUILD
# ============================================================

def load_and_build(
    uploaded_file,
    sheet_name,
):
    """
    One controlled loading path.

    Streamlit UploadedFile
        ↓
    UploadedFileAdapter
        ↓
    data_loader.py
        ↓
    raw data + embedded pictures
        ↓
    calculation layer
        ↓
    unique part layer
    """

    adapted_file = UploadedFileAdapter(
        uploaded_file
    )

    raw_df, picture_map = (
        load_sheet_with_pictures(
            adapted_file,
            sheet_name,
        )
    )

    control_date = pd.Timestamp.today().normalize()

    calc_df = build_calculation_layer(
        raw_df,
        control_date=control_date,
        scan_gap_mode=True,
    )

    unique_df = build_unique_part_dataset(
        calc_df
    )

    unique_df = add_aging_fields(
        unique_df,
        control_date=control_date,
    )

    st.session_state.raw_df = raw_df
    st.session_state.calc_df = calc_df
    st.session_state.unique_df = unique_df
    st.session_state.picture_map = picture_map
    st.session_state.sheet_name = sheet_name
    st.session_state["_npi_state_recovery_notice"] = False

    # A freshly loaded BAQ invalidates any old Enquiry Finder selection.
    st.session_state["selected_enquiry_parts"] = []
    st.session_state["enquiry_search_active"] = False
    st.session_state["enquiry_search_message"] = ""
    st.session_state["enquiry_missing_parts"] = []
    st.session_state["show_enquiry_missing_dialog"] = False
    st.session_state["enquiry_last_query"] = ""
    st.session_state["enquiry_input_text"] = ""
    st.session_state["enquiry_last_input_count"] = 0
    st.session_state["enquiry_last_matched_count"] = 0

    return (
        raw_df,
        calc_df,
        unique_df,
        picture_map,
    )


# ============================================================
# CUSTOMER
# ============================================================

def get_customer_list(df):

    if df is None or df.empty:
        return ["ALL"]

    if "CALC_CUSTOMER" not in df.columns:
        return ["ALL"]

    values = (
        df["CALC_CUSTOMER"]
        .dropna()
        .astype(str)
        .str.strip()
    )

    values = [
        x for x in values.unique()
        if x
    ]

    values = sorted(values)

    return ["ALL"] + values


def apply_customer_filter(
    df,
    customer,
):

    if df is None or df.empty:
        return pd.DataFrame()

    result = df.copy()

    if (
        customer != "ALL"
        and "CALC_CUSTOMER" in result.columns
    ):
        result = result[
            result["CALC_CUSTOMER"]
            .astype(str)
            .str.strip()
            ==
            str(customer).strip()
        ]

    return result


# ============================================================
# CLASSIFICATION
# ============================================================

def apply_classification_filter(
    df,
    classification,
):

    if (
        df is None
        or df.empty
        or classification == "ALL"
    ):
        return df.copy()

    if "CALC_CLASSIFICATION" not in df.columns:
        return df.copy()

    return df[
        df["CALC_CLASSIFICATION"]
        .astype(str)
        .str.strip()
        == classification
    ].copy()


def count_classification(
    df,
    classification,
):

    if (
        df is None
        or df.empty
        or "CALC_CLASSIFICATION"
        not in df.columns
    ):
        return 0

    return int(
        (
            df["CALC_CLASSIFICATION"]
            .astype(str)
            .str.strip()
            == classification
        ).sum()
    )


# ============================================================
# SIDEBAR
# ============================================================

with st.sidebar:

    st.markdown(
        """
        <div style="
            font-size:25px;
            font-weight:800;
            margin-bottom:2px;
        ">
            NPI CONTROL TOWER
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "SHOW THE PAIN → THEN OFFER THE MEDICINE"
    )

    st.divider()

    # --------------------------------------------------------
    # SOURCE DATA
    # --------------------------------------------------------

    st.markdown("### SOURCE DATA")

    uploaded_file = st.file_uploader(
        "Upload Virgin Epicor Web BAQ",
        type=[
            "xlsx",
            "xlsm",
            "xltx",
            "xltm",
            "csv",
        ],
        key="baq_upload",
    )

    if uploaded_file is not None:

        st.caption(
            f"File: {uploaded_file.name}"
        )

        try:

            adapted_file = UploadedFileAdapter(
                uploaded_file
            )

            sheet_names = get_sheet_names(
                adapted_file
            )

            if sheet_names:

                current_sheet = (
                    st.session_state.sheet_name
                )

                if current_sheet in sheet_names:
                    sheet_index = sheet_names.index(
                        current_sheet
                    )
                else:
                    sheet_index = 0

                selected_sheet = st.selectbox(
                    "Sheet",
                    sheet_names,
                    index=sheet_index,
                    key="sheet_selector",
                )

                if st.button(
                    "LOAD / REFRESH BAQ",
                    use_container_width=True,
                ):

                    with st.spinner(
                        "Loading Virgin BAQ..."
                    ):

                        load_and_build(
                            uploaded_file,
                            selected_sheet,
                        )

                    st.success(
                        "BAQ loaded successfully."
                    )

            else:

                st.error(
                    "No worksheet found."
                )

        except Exception as e:

            st.error(
                f"Unable to read BAQ: {e}"
            )

    st.divider()

    # --------------------------------------------------------
    # MODULE
    # --------------------------------------------------------

    st.markdown("### MODULE")

    if st.session_state.get("module_selector") == "Management Commitment":
        st.session_state["module_selector"] = "Control Tower"

    module = st.radio(
        "Select module",
        [
            "Control Tower",
            "Data Validation",
            "Unique-Part Validation",
            "Calculation Layer",
        ],
        key="module_selector",
        label_visibility="collapsed",
    )

    st.divider()


# ============================================================
# DATA AVAILABLE?
# ============================================================

raw_df = st.session_state.raw_df
calc_df = st.session_state.calc_df
unique_df = st.session_state.unique_df
picture_map = st.session_state.picture_map

control_date = pd.Timestamp.today().normalize()

selected_customer = (
    st.session_state.selected_customer
)

if (
    raw_df is None
    or calc_df is None
    or unique_df is None
):

    st.title("NPI CONTROL TOWER")

    if st.session_state.get("_npi_state_recovery_notice", False):
        st.info(
            "Your login session was restored. The previous BAQ data was not "
            "available after the app server restarted, so please upload the BAQ again."
        )

    st.info(
        "Upload the Virgin Epicor Web BAQ "
        "from the sidebar, then click "
        "\"LOAD / REFRESH BAQ\"."
    )

    st.stop()


# ============================================================
# FILTERED DATA
# ============================================================

filtered_unique = apply_customer_filter(
    unique_df,
    selected_customer,
)

filtered_calc = apply_customer_filter(
    calc_df,
    selected_customer,
)


# ============================================================
# TITLE
# ============================================================

def render_title(
    title,
    subtitle=None,
):

    st.markdown(
        f"""
        <div style="
            font-size:30px;
            font-weight:800;
            margin-bottom:4px;
        ">
            {html_escape(title)}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if subtitle:
        st.caption(subtitle)


# ============================================================
# PERSISTENT UI CONTROLLER
# ============================================================

def render_ui_controller(
    active=True,
    jump_id=0,
):
    """
    Persistent parent-document controller.

    IMPORTANT:
    components.html() creates an iframe. Therefore this
    controller does NOT keep its state inside that iframe.

    Instead it installs one controller object in:
        window.parent.__npiControlTowerUI

    That object survives normal Streamlit reruns.

    Responsibilities:
    1. Executive Status → Drilldown jump
    2. Back To Top
    3. Rebind after customer change / BAQ reload
    4. Unlimited repeated actions
    """

    active_js = "true" if active else "false"

    try:
        jump_id_int = int(jump_id)
    except Exception:
        jump_id_int = 0

    components.html(
        f"""
        <script>
        (function () {{

            try {{

                const parentWin =
                    window.parent;

                const parentDoc =
                    parentWin.document;

                const CURRENT_JUMP_ID =
                    {jump_id_int};

                const CONTROLLER_KEY =
                    "__npiControlTowerUI";


                if (!parentWin[CONTROLLER_KEY]) {{

                    parentWin[CONTROLLER_KEY] = {{

                        lastJumpId: 0,

                        observer: null,

                        observerTimer: null,

                        initialized: false,

                        active: false,

                        buttonId:
                            "control-tower-back-to-top",

                        ensureButton: null,

                        jumpToDrilldown: null,

                        requestJump: null,

                        backToTop: null

                    }};

                }}


                const controller =
                    parentWin[CONTROLLER_KEY];


                function findScrollHosts() {{

                    const hosts = [];

                    const selectors = [

                        '[data-testid="stAppViewContainer"]',

                        '[data-testid="stMain"]',

                        'section.main',

                        'main'

                    ];


                    for (
                        const selector
                        of selectors
                    ) {{

                        try {{

                            const element =
                                parentDoc.querySelector(
                                    selector
                                );

                            if (
                                element
                                &&
                                !hosts.includes(
                                    element
                                )
                            ) {{

                                hosts.push(
                                    element
                                );

                            }}

                        }} catch (e) {{}}

                    }}


                    try {{

                        if (
                            parentDoc.scrollingElement
                            &&
                            !hosts.includes(
                                parentDoc.scrollingElement
                            )
                        ) {{

                            hosts.push(
                                parentDoc.scrollingElement
                            );

                        }}

                    }} catch (e) {{}}


                    try {{

                        if (
                            parentDoc.documentElement
                            &&
                            !hosts.includes(
                                parentDoc.documentElement
                            )
                        ) {{

                            hosts.push(
                                parentDoc.documentElement
                            );

                        }}

                    }} catch (e) {{}}


                    try {{

                        if (
                            parentDoc.body
                            &&
                            !hosts.includes(
                                parentDoc.body
                            )
                        ) {{

                            hosts.push(
                                parentDoc.body
                            );

                        }}

                    }} catch (e) {{}}


                    return hosts;

                }}


                function backToTop() {{

                    try {{

                        const topAnchor =
                            parentDoc.getElementById(
                                "npi-page-top-anchor"
                            );


                        if (topAnchor) {{

                            try {{

                                topAnchor.scrollIntoView({{
                                    behavior:
                                        "smooth",

                                    block:
                                        "start"
                                }});

                            }} catch (e) {{}}

                        }}


                        const hosts =
                            findScrollHosts();


                        for (
                            const host
                            of hosts
                        ) {{

                            try {{

                                if (
                                    typeof host.scrollTo
                                    === "function"
                                ) {{

                                    host.scrollTo({{
                                        top: 0,

                                        behavior:
                                            "smooth"
                                    }});

                                }} else {{

                                    host.scrollTop =
                                        0;

                                }}

                            }} catch (e) {{}}

                        }}


                        try {{

                            parentWin.scrollTo({{
                                top: 0,

                                behavior:
                                    "smooth"
                            }});

                        }} catch (e) {{}}

                    }} catch (e) {{}}

                }}


                function jumpToDrilldown() {{

                    try {{

                        const target =
                            parentDoc.getElementById(
                                "npi-drilldown-anchor"
                            );


                        if (!target) {{

                            return false;

                        }}


                        target.scrollIntoView({{
                            behavior:
                                "smooth",

                            block:
                                "start",

                            inline:
                                "nearest"
                        }});


                        return true;

                    }} catch (e) {{

                        return false;

                    }}

                }}


                function ensureButton() {{

                    try {{

                        let button =
                            parentDoc.getElementById(
                                controller.buttonId
                            );


                        if (!button) {{

                            button =
                                parentDoc.createElement(
                                    "button"
                                );

                            button.id =
                                controller.buttonId;

                            button.type =
                                "button";

                            button.innerHTML =
                                "↑ BACK TO TOP";


                            button.style.position =
                                "fixed";

                            button.style.right =
                                "28px";

                            button.style.bottom =
                                "58px";

                            button.style.zIndex =
                                "2147483647";

                            button.style.padding =
                                "9px 14px";

                            button.style.border =
                                "1px solid #999";

                            button.style.borderRadius =
                                "6px";

                            button.style.background =
                                "#ffffff";

                            button.style.color =
                                "#222222";

                            button.style.fontFamily =
                                "Arial,sans-serif";

                            button.style.fontSize =
                                "12px";

                            button.style.fontWeight =
                                "700";

                            button.style.cursor =
                                "pointer";

                            button.style.boxShadow =
                                "0 2px 8px rgba(0,0,0,0.18)";

                            button.style.whiteSpace =
                                "nowrap";


                            parentDoc.body.appendChild(
                                button
                            );

                        }}


                        button.onclick =
                            function () {{

                                backToTop();

                            }};


                        button.style.display =
                            {active_js}
                            ? "block"
                            : "none";


                        return true;

                    }} catch (e) {{

                        return false;

                    }}

                }}


                function requestJump(
                    requestedJumpId
                ) {{

                    if (
                        !requestedJumpId
                        ||
                        requestedJumpId <= 0
                    ) {{

                        return;

                    }}


                    if (
                        requestedJumpId
                        === controller.lastJumpId
                    ) {{

                        return;

                    }}


                    controller.lastJumpId =
                        requestedJumpId;


                    const retryDelays = [
                        0,
                        80,
                        180,
                        350,
                        600,
                        900,
                        1300
                    ];


                    for (
                        const delay
                        of retryDelays
                    ) {{

                        setTimeout(
                            function () {{

                                jumpToDrilldown();

                            }},
                            delay
                        );

                    }}

                }}


                controller.ensureButton =
                    ensureButton;

                controller.jumpToDrilldown =
                    jumpToDrilldown;

                controller.requestJump =
                    requestJump;

                controller.backToTop =
                    backToTop;

                controller.active =
                    {active_js};


                if (
                    !controller.observer
                ) {{

                    try {{

                        controller.observer =
                            new MutationObserver(
                                function () {{

                                    clearTimeout(
                                        controller.observerTimer
                                    );


                                    controller.observerTimer =
                                        setTimeout(
                                            function () {{

                                                try {{

                                                    controller.ensureButton();

                                                }} catch (e) {{}}

                                            }},
                                            80
                                        );

                                }}
                            );


                        controller.observer.observe(
                            parentDoc.body,
                            {{
                                childList:
                                    true,

                                subtree:
                                    true
                            }}
                        );

                    }} catch (e) {{}}

                }}


                ensureButton();


                setTimeout(
                    ensureButton,
                    100
                );

                setTimeout(
                    ensureButton,
                    400
                );

                setTimeout(
                    ensureButton,
                    900
                );


                requestJump(
                    CURRENT_JUMP_ID
                );


                controller.initialized =
                    true;


            }} catch (e) {{}}

        }})();
        </script>
        """,
        height=1,
        scrolling=False,
    )


# ============================================================
# EXECUTIVE STATUS CONFIG
# ============================================================

EXECUTIVE_STATUS = [
    {
        "key": "engineering",
        "label": "ENGINEERING",
        "classification": "RED - ENGINEERING",
        "color": "#D9534F",
    },
    {
        "key": "process",
        "label": "PROCESS",
        "classification": "RED - PROCESS",
        "color": "#F0A202",
    },
    {
        "key": "production",
        "label": "PRODUCTION",
        "classification": "RED - PRODUCTION",
        "color": "#D4A017",
    },
    {
        "key": "on_track",
        "label": "ON TRACK",
        "classification": "GREEN - ON TRACK",
        "color": "#55A868",
    },
    {
        "key": "complete",
        "label": "COMPLETE",
        "classification": "GREEN - COMPLETE",
        "color": "#4C78A8",
    },
]


# ============================================================
# DONUT SVG HELPERS
# ============================================================

def polar_to_cartesian(
    cx,
    cy,
    radius,
    angle_degrees,
):
    angle_radians = math.radians(
        angle_degrees
    )

    return (
        cx
        + radius * math.cos(angle_radians),
        cy
        + radius * math.sin(angle_radians),
    )


def describe_arc(
    cx,
    cy,
    outer_radius,
    inner_radius,
    start_angle,
    end_angle,
):
    """
    Creates a donut segment using an SVG path.
    """

    outer_start = polar_to_cartesian(
        cx,
        cy,
        outer_radius,
        start_angle,
    )

    outer_end = polar_to_cartesian(
        cx,
        cy,
        outer_radius,
        end_angle,
    )

    inner_end = polar_to_cartesian(
        cx,
        cy,
        inner_radius,
        end_angle,
    )

    inner_start = polar_to_cartesian(
        cx,
        cy,
        inner_radius,
        start_angle,
    )

    angle_size = (
        end_angle - start_angle
    )

    large_arc_flag = (
        1
        if angle_size > 180
        else 0
    )

    return (
        f"M {outer_start[0]:.3f} "
        f"{outer_start[1]:.3f} "

        f"A {outer_radius} "
        f"{outer_radius} "
        f"0 "
        f"{large_arc_flag} "
        f"1 "
        f"{outer_end[0]:.3f} "
        f"{outer_end[1]:.3f} "

        f"L {inner_end[0]:.3f} "
        f"{inner_end[1]:.3f} "

        f"A {inner_radius} "
        f"{inner_radius} "
        f"0 "
        f"{large_arc_flag} "
        f"0 "
        f"{inner_start[0]:.3f} "
        f"{inner_start[1]:.3f} "

        "Z"
    )

def build_donut_svg(
    status_items,
    total,
):
    """
    Builds the Executive Status donut.

    CLICK BEHAVIOUR
    ----------------
    Donut section -> selected classification
    Donut center   -> ALL UNIQUE PARTS

    Uses top-level URL navigation instead of attempting
    to click Streamlit widgets through the components.html()
    iframe.
    """

    from urllib.parse import quote

    width = 300
    height = 270

    cx = width / 2
    cy = height / 2

    outer_radius = 96
    inner_radius = 58

    current_angle = -90

    svg_parts = []

    # --------------------------------------------------------
    # URL HELPER
    # --------------------------------------------------------

    def make_jump_url(value):
        return (
            "?npi_exec="
            + quote(
                str(value),
                safe="",
            )
        )

    # --------------------------------------------------------
    # SVG START
    # --------------------------------------------------------

    svg_parts.append(
        f"""
        <svg
            width="{width}"
            height="{height}"
            viewBox="0 0 {width} {height}"
            xmlns="http://www.w3.org/2000/svg"
            style="
                display:block;
                width:100%;
                max-width:{width}px;
                height:auto;
                margin:auto;
                overflow:visible;
            "
        >
        """
    )

    # --------------------------------------------------------
    # EMPTY DONUT
    # --------------------------------------------------------

    if total <= 0:

        svg_parts.append(
            f"""
            <circle
                cx="{cx}"
                cy="{cy}"
                r="{(
                    outer_radius
                    + inner_radius
                ) / 2}"
                fill="none"
                stroke="#E5E7EB"
                stroke-width="{(
                    outer_radius
                    - inner_radius
                )}"
            />
            """
        )

    # --------------------------------------------------------
    # DONUT SECTIONS
    # --------------------------------------------------------

    else:

        for item in status_items:

            value = int(
                item["value"]
            )

            if value <= 0:
                continue

            portion = (
                value / total
            )

            angle_span = (
                portion * 360
            )

            gap = (
                1.4
                if angle_span < 359
                else 0
            )

            start_angle = (
                current_angle + gap
            )

            end_angle = (
                current_angle
                + angle_span
                - gap
            )

            path = describe_arc(
                cx,
                cy,
                outer_radius,
                inner_radius,
                start_angle,
                end_angle,
            )

            classification = (
                item["classification"]
            )

            escaped_classification = (
                html_escape(
                    classification
                )
            )

            jump_url = (
                make_jump_url(
                    classification
                )
            )

            # ------------------------------------------------
            # EACH DONUT SECTION IS NOW A REAL LINK
            # ------------------------------------------------

            svg_parts.append(
                f"""
                <a
                    href="{jump_url}"
                    target="_top"
                    style="
                        cursor:pointer;
                    "
                    aria-label="{escaped_classification}"
                >
                    <path
                        d="{path}"
                        fill="{item['color']}"
                        stroke="#FFFFFF"
                        stroke-width="2"
                        data-classification="{escaped_classification}"
                        style="
                            cursor:pointer;
                            transition:
                                opacity 0.15s,
                                filter 0.15s;
                        "
                        onmouseover="
                            this.style.opacity='0.78';
                            this.style.filter='brightness(1.08)';
                        "
                        onmouseout="
                            this.style.opacity='1';
                            this.style.filter='none';
                        "
                    ></path>
                </a>
                """
            )

            current_angle += (
                angle_span
            )

    # ========================================================
    # CENTER
    # ========================================================

    # The entire center is one clickable link.
    # Clicking it selects ALL UNIQUE PARTS.

    all_jump_url = make_jump_url(
        "ALL"
    )

    svg_parts.append(
        f"""
        <a
            href="{all_jump_url}"
            target="_top"
            style="
                cursor:pointer;
                text-decoration:none;
            "
            aria-label="ALL UNIQUE PARTS"
        >

            <circle
                cx="{cx}"
                cy="{cy}"
                r="{inner_radius - 1}"
                fill="#FFFFFF"
                style="
                    cursor:pointer;
                "
            />

            <text
                x="{cx}"
                y="{cy - 8}"
                text-anchor="middle"
                dominant-baseline="middle"
                font-family="Arial,sans-serif"
                font-size="25"
                font-weight="800"
                fill="#222222"
                style="
                    cursor:pointer;
                    user-select:none;
                "
            >
                {fmt_number(total)}
            </text>

            <text
                x="{cx}"
                y="{cy + 19}"
                text-anchor="middle"
                dominant-baseline="middle"
                font-family="Arial,sans-serif"
                font-size="9"
                font-weight="700"
                letter-spacing="0.6"
                fill="#666666"
                style="
                    cursor:pointer;
                    user-select:none;
                "
            >
                TOTAL UNIQUE
            </text>

            <text
                x="{cx}"
                y="{cy + 34}"
                text-anchor="middle"
                dominant-baseline="middle"
                font-family="Arial,sans-serif"
                font-size="9"
                font-weight="700"
                letter-spacing="0.6"
                fill="#666666"
                style="
                    cursor:pointer;
                    user-select:none;
                "
            >
                PARTS
            </text>

        </a>
        """
    )

    # --------------------------------------------------------
    # SVG END
    # --------------------------------------------------------

    svg_parts.append(
        "</svg>"
    )

    return "".join(
        svg_parts
    )


# ============================================================
# EXECUTIVE STATUS
# ============================================================

def _batch_a_touch_script(kind, root_selector):
    """Scoped presentation-only touch routing; no data or persistence operations."""
    return r"""
(function(win,config){
    'use strict';
    const doc=win.document, key='__npiBatchATouch_'+config.kind;
    if(win[key] && win[key].dispose)win[key].dispose();
    let gesture=null,blockUntil=0;
    const removals=[];
    function listen(target,type,fn,options){if(!target || !target.addEventListener)return;target.addEventListener(type,fn,options);removals.push(()=>target.removeEventListener(type,fn,options));}
    function root(){return doc.querySelector(config.root);}
    function canScroll(element,view){
        if(!element || element.nodeType!==1)return false;
        const overflow=view.getComputedStyle(element).overflowY;
        return (/auto|scroll/.test(overflow) || element.matches('[data-testid="stMain"],section.main')) && element.scrollHeight>element.clientHeight+1;
    }
    function hasRoom(el,delta){return delta>0 ? el.scrollTop<el.scrollHeight-el.clientHeight-1 : el.scrollTop>1;}
    function ownerFor(target,delta){
        // Native scrolling owns any eligible container in this document.
        for(let el=target;el;el=el.parentElement){if(canScroll(el,win) && hasRoom(el,delta))return null;}
        const scrolling=doc.scrollingElement;
        if(scrolling && !/hidden|clip/.test(win.getComputedStyle(scrolling).overflowY) && hasRoom(scrolling,delta))return null;
        // An iframe cannot reliably chain a native pan into Streamlit. Select
        // ONE parent owner, once per gesture; never write a chain of ancestors.
        let view=win;
        for(let depth=0;depth<6;depth++){
            try{
                if(view.parent===view)break;
                let node=view.frameElement;view=view.parent;
                for(let el=node;el;el=el.parentElement){if(canScroll(el,view) && hasRoom(el,delta))return el;}
                const parentScroll=view.document.scrollingElement;
                if(parentScroll && !/hidden|clip/.test(view.getComputedStyle(parentScroll).overflowY) && hasRoom(parentScroll,delta))return parentScroll;
            }catch(e){break;}
        }
        return null;
    }
    function restoreAccidentalFocus(g){
        const active=doc.activeElement;
        if(active && active!==g.active && active.matches && active.matches('input,textarea,select') && g.target.contains && g.target.contains(active)){
            active.blur();
            try{if(g.active && g.active.focus)g.active.focus({preventScroll:true});}catch(e){}
        }
    }
    function start(event){
        blockUntil=0;
        const area=root(),point=event.touches && event.touches[0];
        if(!area || !point || event.touches.length!==1 || !area.contains(event.target) ||
            (event.target.closest && event.target.closest('#npi-dashboard-drilldown-boundary,[role="separator"]'))){gesture=null;return;}
        // screen coordinates stay stable when scrolling moves the iframe.
        gesture={x:point.screenX,y:point.screenY,lastY:point.screenY,axis:null,owner:null,decided:false,moved:false,target:event.target,active:doc.activeElement};
        try{win.parent.__npiTouchActive=true;}catch(e){}
    }
    function move(event){
        const g=gesture;if(!g)return;
        if(!event.touches || event.touches.length!==1){finish(event);return;}
        const point=event.touches[0],dx=point.screenX-g.x,dy=point.screenY-g.y;
        const ax=Math.abs(dx),ay=Math.abs(dy);
        if(Math.max(ax,ay)>=10){g.moved=true;blockUntil=Date.now()+700;restoreAccidentalFocus(g);}
        if(!g.decided && Math.max(ax,ay)>=3){
            g.decided=true;g.axis=ay>ax*1.1?'y':'x';
            if(g.axis==='y' && event.cancelable)g.owner=ownerFor(g.target,-dy);
        }
        if(g.owner){
            // Losing cancellation means native owns the rest of this gesture.
            // Never apply a programmatic correction to an uncancellable event.
            if(!event.cancelable){g.owner=null;}
            else{
                event.preventDefault();
                const delta=g.lastY-point.screenY;
                g.owner.scrollTop=Math.max(0,Math.min(g.owner.scrollHeight-g.owner.clientHeight,g.owner.scrollTop+delta));
            }
        }
        g.lastY=point.screenY;
    }
    function finish(event){
        const g=gesture;gesture=null;
        try{win.parent.__npiTouchActive=false;}catch(e){}
        if(g && g.moved){blockUntil=Date.now()+700;restoreAccidentalFocus(g);}
    }
    function guardClick(event){
        if(Date.now()<blockUntil){event.preventDefault();event.stopImmediatePropagation();}
    }
    listen(doc,'touchstart',start,{capture:true,passive:true});
    listen(doc,'touchmove',move,{capture:true,passive:false});
    listen(doc,'touchend',finish,{capture:true,passive:true});
    listen(doc,'touchcancel',finish,{capture:true,passive:true});
    listen(doc,'click',guardClick,true);listen(doc,'dblclick',guardClick,true);
    listen(win,'pointerdown',function(event){if(event.pointerType==='mouse')blockUntil=0;},true);
    listen(win,'resize',finish);listen(win,'orientationchange',finish);
    const dispose=function(){finish({});removals.forEach(fn=>fn());};
    listen(win,'pagehide',dispose,{once:true});
    win[key]={dispose};
})(window,__CONFIG__);

""".replace("__CONFIG__", json.dumps({"kind": kind, "root": root_selector}))


def render_executive_status(
    unique_df,
    delay_bucket_summary=None,
    hotspot_df=None,
):
    """
    TOP-01 EXECUTIVE STATUS

    FIX-3

    Floating / draggable / resizable layout.

    IMPORTANT:
    - Calculation layer remains untouched.
    - Unique-part dataset remains authoritative.
    - Donut size is locked at 305px.
    - Executive Status is rendered only once.
    - Streamlit bridge buttons are hidden via CSS/DOM.
    - Legend tiles remain clickable.
    - Donut sections remain clickable.
    - Clicks use real Streamlit bridge buttons.
    - Bridge buttons update selected_classification.
    - Layout is stored locally in the browser.
    """

    # ============================================================
    # 1. BUILD EXECUTIVE STATUS DATA
    # ============================================================

    df = unique_df.copy()

    selected_customer = st.session_state.get(
        "selected_customer",
        "ALL",
    )

    if (
        selected_customer != "ALL"
        and "Customer" in df.columns
    ):
        df = df[
            df["Customer"]
            .fillna("")
            .astype(str)
            .str.strip()
            .eq(
                str(selected_customer).strip()
            )
        ].copy()

    total = len(df)

    status_items = []

    for item in EXECUTIVE_STATUS:

        classification = item[
            "classification"
        ]

        value = 0

        if "CLASSIFICATION" in df.columns:

            value = int(
                (
                    df["CLASSIFICATION"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .eq(classification)
                ).sum()
            )

        elif "CALC_CLASSIFICATION" in df.columns:

            value = int(
                (
                    df["CALC_CLASSIFICATION"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .eq(classification)
                ).sum()
            )

        status_items.append(
            {
                **item,
                "value": value,
            }
        )

    # ============================================================
    # 2. PREPARE LEGEND DATA
    # ============================================================

    legend_items = []

    for item in status_items:

        value = int(
            item["value"]
        )

        percentage = (
            (value / total) * 100
            if total > 0
            else 0
        )

        label = item["label"]

        legend_items.append(
            {
                "key": item["key"],
                "label": label,
                "classification": item[
                    "classification"
                ],
                "color": item["color"],
                "value": value,
                "percentage": percentage,
            }
        )

    # ============================================================
    # 3. DONUT SVG
    # ============================================================

    donut_svg = build_donut_svg(
        status_items,
        total,
    )

    # ============================================================
    # 4. STREAMLIT BRIDGE CALLBACK
    #
    # THIS IS THE FIX.
    #
    # The hidden Streamlit buttons are the real Streamlit
    # interaction bridge.
    #
    # When the floating donut / tile clicks the bridge button,
    # this callback updates selected_classification and causes
    # Streamlit to rerun.
    # ============================================================

    def select_executive_classification(
        classification,
    ):

        st.session_state[
            "selected_classification"
        ] = classification
        st.session_state["selected_delay_bucket"] = ""
        st.session_state["selected_hotspot"] = ""
        st.session_state["selected_commitment_status"] = ""
        st.session_state["drilldown_current_process_filter"] = "ALL"
        st.session_state["enquiry_search_active"] = False

        # A fresh selection requests one scroll to the drilldown.
        st.session_state[
            "drilldown_jump_id"
        ] = int(
            st.session_state.get(
                "drilldown_jump_id",
                0,
            )
        ) + 1

        # Remove any older URL-driven selection so it cannot
        # override this newer legend interaction on a later rerun.
        if "npi_exec" in st.query_params:
            del st.query_params["npi_exec"]

    # ============================================================
    # 5. HIDDEN STREAMLIT BRIDGE BUTTONS
    #
    # IMPORTANT:
    # These buttons are NOT intended to be visible.
    #
    # They remain real Streamlit buttons so the existing
    # Streamlit rerun mechanism is preserved.
    # ============================================================

    bridge_container = st.container()

    with bridge_container:

        st.markdown(
            """
            <div
                id="npi-exec-bridge-start"
                style="
                    display:none !important;
                    width:0 !important;
                    height:0 !important;
                    margin:0 !important;
                    padding:0 !important;
                    overflow:hidden !important;
                "
            ></div>
            """,
            unsafe_allow_html=True,
        )

        # Dedicated ALL bridge for the clickable donut center.
        # This is a real Streamlit widget, unlike the SVG center itself.
        st.button(
            "ALL",
            key="floating_exec_ALL",
            on_click=select_executive_classification,
            args=("ALL",),
        )

        for item in legend_items:

            st.button(
                item["label"],
                key=(
                    "floating_exec_"
                    + item["key"]
                ),
                use_container_width=True,
                on_click=(
                    select_executive_classification
                ),
                args=(
                    item["classification"],
                ),
            )

        # Hidden real Streamlit bridge buttons for the clickable bucket tiles.
        # Selecting a bucket filters Drilldown and requests a smooth jump to it.
        if delay_bucket_summary is not None:
            def select_delay_bucket(bucket):
                st.session_state["selected_delay_bucket"] = bucket
                st.session_state["selected_hotspot"] = ""
                st.session_state["selected_commitment_status"] = ""
                st.session_state["selected_classification"] = "ALL"
                st.session_state["drilldown_current_process_filter"] = "ALL"
                st.session_state["enquiry_search_active"] = False
                st.session_state["drilldown_jump_id"] = int(
                    st.session_state.get("drilldown_jump_id", 0)
                ) + 1
                if "npi_exec" in st.query_params:
                    del st.query_params["npi_exec"]

            for bucket_label in delay_bucket_summary["Part Delay Days Bucket"].astype(str).tolist():
                st.button(
                    bucket_label,
                    key="floating_delay_bucket_" + bucket_label,
                    on_click=select_delay_bucket,
                    args=(bucket_label,),
                )

        # Hidden real Streamlit buttons for Production Hotspot row clicks.
        def select_production_hotspot(operation):
            st.session_state["selected_hotspot"] = operation
            st.session_state["selected_delay_bucket"] = ""
            st.session_state["selected_commitment_status"] = ""
            st.session_state["selected_classification"] = "ALL"
            st.session_state["drilldown_current_process_filter"] = "ALL"
            st.session_state["enquiry_search_active"] = False
            st.session_state["drilldown_jump_id"] = int(
                st.session_state.get("drilldown_jump_id", 0)
            ) + 1
            if "npi_exec" in st.query_params:
                del st.query_params["npi_exec"]

        if hotspot_df is not None and not hotspot_df.empty:
            operation_col = first_existing_column(
                hotspot_df,
                ["CURRENT_OPERATION", "Current Operation", "Current Process", "CURRENT PROCESS", "CALC_CURRENT_OPERATION", "Operation"],
            )
            if operation_col is not None:
                for operation in hotspot_df[operation_col].dropna().astype(str).drop_duplicates().tolist():
                    st.button(
                        operation,
                        key="floating_hotspot_" + operation,
                        on_click=select_production_hotspot,
                        args=(operation,),
                    )

        # Hidden real Streamlit bridge buttons for Delay Commitment tile clicks.
        # These preserve the existing proven iframe-to-Streamlit click mechanism.
        def select_delay_commitment_status(status):
            st.session_state["selected_commitment_status"] = status
            st.session_state["selected_classification"] = "ALL"
            st.session_state["selected_delay_bucket"] = ""
            st.session_state["selected_hotspot"] = ""
            st.session_state["drilldown_current_process_filter"] = "ALL"
            st.session_state["enquiry_search_active"] = False
            st.session_state["selected_enquiry_parts"] = []
            st.session_state["drilldown_jump_id"] = int(
                st.session_state.get("drilldown_jump_id", 0)
            ) + 1
            for page_key in list(st.session_state.keys()):
                if str(page_key).startswith("drilldown_page_state_"):
                    st.session_state[page_key] = 1
            if "npi_exec" in st.query_params:
                del st.query_params["npi_exec"]

        for commitment_status in ["OVERDUE", "DUE TODAY", "UPCOMING", "NO COMMITMENT"]:
            bridge_text = "DELAY COMMITMENT BRIDGE " + commitment_status
            st.button(
                bridge_text,
                key="floating_delay_commitment_bridge_" + commitment_status.lower().replace(" ", "_"),
                on_click=select_delay_commitment_status,
                args=(commitment_status,),
            )

        st.markdown(
            """
            <div
                id="npi-exec-bridge-end"
                style="
                    display:none !important;
                    width:0 !important;
                    height:0 !important;
                    margin:0 !important;
                    padding:0 !important;
                    overflow:hidden !important;
                "
            ></div>
            """,
            unsafe_allow_html=True,
        )

    # ============================================================
    # 6. LEGEND HTML
    # ============================================================

    legend_html = ""

    for item in legend_items:

        classification = html.escape(
            item["classification"]
        )

        label = html.escape(
            item["label"]
        )

        value = fmt_number(
            item["value"]
        )

        percentage = (
            f"{item['percentage']:.1f}%"
        )

        color = item["color"]

        legend_html += f"""
            <button
                type="button"
                class="npi-float-legend"
                data-classification="{classification}"
                data-label="{label}"
                style="--tile-color:{color};"
            >

                <span
                    class="npi-float-strip"
                ></span>

                <span
                    class="npi-float-label"
                >
                    {label}
                </span>

                <span
                    class="npi-float-count"
                >
                    {value}
                </span>

                <span
                    class="npi-float-percent"
                >
                    ({percentage})
                </span>

            </button>
        """

    # ============================================================
    # 6B. DELAY BUCKET TILES
    # ============================================================

    bucket_tiles_html = ""
    if delay_bucket_summary is not None and not delay_bucket_summary.empty:
        bucket_total = int(delay_bucket_summary["UNIQUE PARTS"].sum())
        selected_bucket = st.session_state.get("selected_delay_bucket", "")
        bucket_severity_colors = {
            "<30 DAYS": ("#E4F5E8", "#8CC99B"),
            "30–90 DAYS": ("#F0F5D9", "#C4D58B"),
            "3–6 MONTHS": ("#FFF2C6", "#E4C66D"),
            "6–12 MONTHS": ("#FFE2BD", "#E9AD67"),
            "1–2 YEARS": ("#FFD0C4", "#E58C78"),
            ">2 YEARS": ("#F8B9B9", "#D96C6C"),
        }
        for _, bucket_row in delay_bucket_summary.iterrows():
            bucket_label = str(bucket_row["Part Delay Days Bucket"])
            bucket_count = int(bucket_row["UNIQUE PARTS"])
            bucket_pct = (bucket_count / bucket_total * 100) if bucket_total else 0.0
            selected_class = " is-selected" if bucket_label == selected_bucket else ""
            bucket_bg, bucket_border = bucket_severity_colors.get(
                bucket_label, ("#FFFFFF", "#D7DADF")
            )
            bucket_style = f"--bucket-bg:{bucket_bg};--bucket-border:{bucket_border};"
            bucket_tiles_html += f"""
                <button type="button" class="npi-delay-bucket-tile{selected_class}" data-bucket="{html.escape(bucket_label, quote=True)}" style="{bucket_style}">
                    <span class="npi-delay-bucket-label">{html.escape(bucket_label)}</span>
                    <span class="npi-delay-bucket-count">{bucket_count:,}</span>
                    <span class="npi-delay-bucket-percent">({bucket_pct:.1f}%)</span>
                </button>
            """

    # ============================================================
    # 6C. PRODUCTION HOTSPOT ROWS
    # ============================================================

    hotspot_rows_html = ""
    if hotspot_df is not None and not hotspot_df.empty:
        operation_col = first_existing_column(
            hotspot_df,
            ["CURRENT_OPERATION", "Current Operation", "Current Process", "CURRENT PROCESS", "CALC_CURRENT_OPERATION", "Operation"],
        )
        count_col = first_existing_column(
            hotspot_df,
            ["UNIQUE_PARTS", "UNIQUE PARTS", "Unique Parts", "PART COUNT", "Part Count", "COUNT"],
        )
        if operation_col is not None:
            for _, hotspot_row in hotspot_df.iterrows():
                operation = str(hotspot_row.get(operation_col, "")).strip()
                if not operation or operation.lower() == "nan":
                    continue
                count_text = ""
                if count_col is not None:
                    try:
                        count_text = f"{int(hotspot_row[count_col]):,}"
                    except (TypeError, ValueError):
                        count_text = str(hotspot_row[count_col])
                hotspot_rows_html += ('<button type="button" class="npi-hotspot-row" data-hotspot="' + html.escape(operation, quote=True) + '"><span class="npi-hotspot-operation">' + html.escape(operation) + '</span><span class="npi-hotspot-count">' + html.escape(count_text) + '</span></button>')

    # ============================================================
    # 6D. DELAY COMMITMENT TILES
    # ============================================================

    commitment_status_map = build_delay_commitment_status_map(
        df,
        st.session_state.get("actions_df"),
        pd.Timestamp.today().normalize(),
    )
    commitment_status_order = [
        "OVERDUE",
        "DUE TODAY",
        "UPCOMING",
        "NO COMMITMENT",
    ]
    commitment_status_styles = {
        "OVERDUE": ("#FDECEC", "#E7A2A2", "#BA3F48", "!"),
        "DUE TODAY": ("#FFF3DD", "#EAC77F", "#98630F", "•"),
        "UPCOMING": ("#EAF3FF", "#B9D2F0", "#3268A2", "↗"),
        "NO COMMITMENT": ("#F0EEFB", "#D0C8F2", "#6552A0", "?"),
    }
    selected_commitment_status = st.session_state.get("selected_commitment_status", "")
    commitment_tiles_html = ""
    for commitment_status in commitment_status_order:
        count = sum(1 for item_status in commitment_status_map.values() if item_status == commitment_status)
        tile_bg, tile_border, tile_accent, icon_text = commitment_status_styles[commitment_status]
        selected_class = " is-selected" if selected_commitment_status == commitment_status else ""
        bridge_text = "DELAY COMMITMENT BRIDGE " + commitment_status
        commitment_tiles_html += f"""
            <button type="button"
                    class="npi-delay-commitment-tile{selected_class}"
                    data-status="{html.escape(commitment_status, quote=True)}"
                    data-bridge-label="{html.escape(bridge_text, quote=True)}"
                    style="--commitment-bg:{tile_bg};--commitment-border:{tile_border};--commitment-accent:{tile_accent};">
                <span class="npi-commitment-icon">{icon_text}</span>
                <span class="npi-commitment-label">{html.escape(commitment_status)}</span>
                <span class="npi-commitment-count">{count:,}</span>
                <span class="npi-commitment-unit">PARTS</span>
            </button>
        """

    # ============================================================
    # 7. FLOATING HTML
    # ============================================================

    # Opaque, stable per-account layout namespace; auth/access code is unchanged.
    dashboard_layout_owner = hashlib.sha256(
        ("npi-dashboard-layout-v5:" + str(st.session_state.get("npi_user_id", ""))).encode("utf-8")
    ).hexdigest()[:32]

    floating_html = f"""
<style>

* {{
    box-sizing: border-box;
}}

html,
body {{
    margin: 0;
    padding: 0;

    background: transparent;

    overflow: hidden;
}}

/* ============================================================
   STAGE
   ============================================================ */

.npi-floating-stage {{
    position: relative;

    width: 100%;
    height: 553px;

    margin: 0;
    padding: 0;

    background: transparent;

    font-family:
        Arial,
        Helvetica,
        sans-serif;
}}

/* Draggable boundary that controls the dashboard iframe height / space before Drilldown. */
.npi-dashboard-drilldown-boundary {{
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: ns-resize;
    touch-action: none;
    user-select: none;
    z-index: 2147483647;
    background: linear-gradient(to bottom, rgba(245,247,250,0), rgba(232,237,244,0.62));
}}
.npi-dashboard-drilldown-boundary-grip {{
    width: 76px;
    height: 5px;
    border-radius: 999px;
    background: #AAB5C3;
    box-shadow: 0 1px 3px rgba(23,40,63,0.18);
    transition: background 120ms ease, width 120ms ease, box-shadow 120ms ease;
}}
.npi-dashboard-drilldown-boundary:hover .npi-dashboard-drilldown-boundary-grip {{
    width: 104px;
    background: #315C83;
    box-shadow: 0 0 0 3px rgba(49,92,131,0.10);
}}

/* ============================================================
   COMMON FLOATING PANEL
   ============================================================ */

.npi-floating-panel {{
    position: absolute;

    min-width: 260px;
    min-height: 120px;

    background: #FFFFFF;

    border:
        1px solid #C9CDD3;

    border-radius: 8px;

    box-shadow:
        0 2px 8px
        rgba(0,0,0,0.10);

    overflow: hidden;

    resize: both;

    z-index: 10;
}}

/* ============================================================
   PANEL HEADER
   ============================================================ */

.npi-floating-header {{
    position: relative;

    height: 38px;

    display: flex;

    align-items: center;

    justify-content: flex-start;

    padding:
        0 12px;

    background: #EEF0F2;

    border-bottom:
        1px solid #D6D9DD;

    color: #222222;

    font-size: 13px;

    font-weight: 800;

    letter-spacing: 0.2px;

    cursor: move;

    user-select: none;
}}

.npi-floating-header-actions {{
    margin-left: auto;

    display: flex;

    align-items: center;

    gap: 5px;
}}

.npi-floating-reset {{
    border:
        1px solid #C7CBD1;

    background: #FFFFFF;

    color: #555555;

    border-radius: 4px;

    height: 24px;

    padding:
        0 7px;

    font-size: 10px;

    font-weight: 700;

    cursor: pointer;
}}

.npi-floating-reset:hover {{
    background: #F5F5F5;
}}

/* ============================================================
   EXECUTIVE CONTENT
   ============================================================ */

.npi-executive-floating-content {{
    width: 100%;

    height:
        calc(100% - 38px);

    padding:
        8px 10px;

    display: flex;

    align-items: center;

    justify-content: center;

    overflow: hidden;
}}

.npi-executive-floating-inner {{
    display: flex;

    flex-direction: row;

    align-items: center;

    justify-content: flex-start;

    gap: 10px;

    width: 100%;

    height: 100%;
}}

/* ============================================================
   DONUT
   ============================================================ */

.npi-floating-donut {{
    flex:
        0 0 260px;

    width: 260px;

    height: 260px;

    display: flex;

    align-items: center;

    justify-content: center;

    overflow: visible;
    margin: 0;
}}

/* ============================================================
   LEGEND
   ============================================================ */

.npi-floating-legend {{
    flex:
        0 0 auto;

    width: max-content;

    min-width: 0;

    display: flex;

    flex-direction: column;

    justify-content: center;

    gap: 3px;

    padding: 0;

    margin: 0;
}}

.npi-float-legend {{
    position: relative;

    width: 100%;

    height: 38px;

    min-height: 38px;

    flex:
        0 0 38px;

    display: flex;

    align-items: center;

    padding:
        0 8px 0 12px;

    margin: 0;

    border:
        1px solid #D7DADF;

    border-radius: 6px;

    background: #FFFFFF;

    color: #222222;

    cursor: pointer;

    font-family:
        Consolas,
        "Courier New",
        monospace;

    font-size: 11px;

    font-weight: 700;

    text-align: left;

    white-space: nowrap;

    overflow: hidden;

    transition:
        background 0.12s,
        box-shadow 0.12s,
        transform 0.12s;
}}

.npi-float-legend:hover {{
    background: #F8F9FA;

    box-shadow:
        0 1px 4px
        rgba(0,0,0,0.08);

    transform:
        translateX(1px);
}}

.npi-float-strip {{
    position: absolute;

    left: 0;
    top: 0;
    bottom: 0;

    width: 7px;

    background:
        var(--tile-color);

    border-radius:
        5px 0 0 5px;
}}

.npi-float-label {{
    display: inline-block;

    width: auto;

    flex: 0 0 auto;

    margin-right: 20px;

    overflow: hidden;

    white-space: nowrap;
}}

.npi-float-count {{
    display: inline-block;

    width: 42px;

    flex:
        0 0 42px;

    text-align: right;

    margin-right: 2px;
}}

.npi-float-percent {{
    display: inline-block;

    width: 58px;

    flex:
        0 0 58px;

    text-align: right;
}}

/* ============================================================
   DELAY BUCKET PANEL
   ============================================================ */

.npi-delay-bucket-content {{
    width: 100%;
    height: calc(100% - 38px);
    padding: 8px;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: repeat(3, minmax(0, 1fr));
    gap: 8px;
    overflow: auto;
}}

.npi-delay-bucket-tile {{
    width: 100%;
    min-width: 0;
    min-height: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 3px;
    padding: 6px 4px;
    border: 1px solid var(--bucket-border, #D7DADF);
    border-radius: 6px;
    background: var(--bucket-bg, #FFFFFF);
    color: #222222;
    font-family: Consolas, "Courier New", monospace;
    font-weight: 700;
    text-align: center;
    cursor: pointer;
    transition: filter 120ms ease, box-shadow 120ms ease;
}}

.npi-delay-bucket-tile:hover {{ filter: brightness(0.97); }}
.npi-delay-bucket-tile.is-selected {{
    border: 2px solid #244F82;
    box-shadow: 0 0 0 2px rgba(36, 79, 130, 0.16);
}}
.npi-delay-bucket-label {{
    display: block;
    max-width: 100%;
    white-space: nowrap;
    font-size: 9px;
    line-height: 1.15;
}}
.npi-delay-bucket-count {{
    display: block;
    font-size: 21px;
    line-height: 1.1;
    text-align: center;
}}
.npi-delay-bucket-percent {{
    display: block;
    font-size: 9px;
    line-height: 1.15;
    white-space: nowrap;
}}

/* ============================================================
   PRODUCTION HOTSPOT
   ============================================================ */
.npi-hotspot-content {{
    width: 100%;
    height: calc(100% - 38px);
    padding: 8px;
    overflow: auto;
}}
.npi-hotspot-row {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) 58px;
    gap: 8px;
    align-items: center;
    width: 100%;
    min-height: 34px;
    margin-bottom: 5px;
    padding: 6px 8px;
    border: 1px solid #D7DADF;
    border-radius: 5px;
    background: #FFFFFF;
    color: #222222;
    font: 700 11px Consolas, "Courier New", monospace;
    text-align: left;
    cursor: pointer;
}}
.npi-hotspot-row:hover {{ background: #F3F6FA; border-color: #8DA7C5; }}
.npi-hotspot-operation {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.npi-hotspot-count {{ text-align: right; }}

/* ============================================================
   PART DELAY DAYS BUCKET — MODERN VISUAL TREATMENT
   Presentation-only CSS. IDs, classes, DOM structure and click
   handlers remain unchanged.
   ============================================================ */
#npi-delay-bucket-panel {{
    border: 1px solid #D8E2EF !important;
    border-radius: 12px !important;
    background: #FFFFFF !important;
    box-shadow: 0 8px 22px rgba(27, 49, 79, 0.10) !important;
}}
#npi-delay-bucket-panel .npi-floating-header {{
    height: 38px;
    min-height: 38px;
    padding: 0 12px;
    gap: 9px;
    background: linear-gradient(90deg, #FFF5E8 0%, #FFFBF5 100%);
    border-bottom: 1px solid #F2E2CD;
    color: #724719;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.85px;
}}
#npi-delay-bucket-panel .npi-floating-header::before {{
    content: "";
    display: block;
    flex: 0 0 4px;
    width: 4px;
    height: 20px;
    border-radius: 999px;
    background: linear-gradient(180deg, #E49B39 0%, #F2C078 100%);
}}
#npi-delay-bucket-panel .npi-delay-bucket-content {{
    padding: 9px;
    gap: 8px;
    background: linear-gradient(145deg, #FFFFFF 0%, #FFFCF7 100%);
}}
#npi-delay-bucket-panel .npi-delay-bucket-tile {{
    border: 1px solid var(--bucket-border, #D7DADF);
    border-radius: 8px;
    box-shadow: 0 2px 5px rgba(42, 55, 71, 0.055);
    color: #28384B;
    font-family: Arial, Helvetica, sans-serif;
    transition: transform 130ms ease, box-shadow 130ms ease, filter 130ms ease;
}}
#npi-delay-bucket-panel .npi-delay-bucket-tile:hover {{
    filter: brightness(0.99);
    transform: translateY(-1px);
    box-shadow: 0 5px 12px rgba(42, 55, 71, 0.11);
}}
#npi-delay-bucket-panel .npi-delay-bucket-tile:focus-visible {{
    outline: 2px solid #376A9E;
    outline-offset: 1px;
}}
#npi-delay-bucket-panel .npi-delay-bucket-tile.is-selected {{
    border: 2px solid #315F91;
    box-shadow: 0 0 0 2px rgba(49, 95, 145, 0.15), 0 4px 10px rgba(42, 55, 71, 0.08);
}}
#npi-delay-bucket-panel .npi-delay-bucket-label {{
    color: #5B6573;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0.35px;
}}
#npi-delay-bucket-panel .npi-delay-bucket-count {{
    color: #1E334D;
    font-size: 22px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    line-height: 1.05;
}}
#npi-delay-bucket-panel .npi-delay-bucket-percent {{
    color: #69778A;
    font-size: 9px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
}}
#npi-delay-bucket-panel .npi-delay-bucket-content::-webkit-scrollbar {{
    width: 7px;
}}
#npi-delay-bucket-panel .npi-delay-bucket-content::-webkit-scrollbar-thumb {{
    background: #D8DEE8;
    border-radius: 999px;
}}

/* ============================================================
   PRODUCTION HOTSPOT — MODERN VISUAL TREATMENT
   Presentation-only CSS. Row-click behavior remains unchanged.
   ============================================================ */
#npi-hotspot-panel {{
    border: 1px solid #D8E2EF !important;
    border-radius: 12px !important;
    background: #FFFFFF !important;
    box-shadow: 0 8px 22px rgba(27, 49, 79, 0.10) !important;
}}
#npi-hotspot-panel .npi-floating-header {{
    height: 38px;
    min-height: 38px;
    padding: 0 12px !important;
    gap: 9px;
    background: linear-gradient(90deg, #F1F6FE 0%, #FBFCFE 100%);
    border-bottom: 1px solid #DCE6F2;
    color: #244466;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.85px;
}}
#npi-hotspot-panel .npi-floating-header::before {{
    content: "";
    display: block;
    flex: 0 0 4px;
    width: 4px;
    height: 20px;
    border-radius: 999px;
    background: linear-gradient(180deg, #4B83C4 0%, #86AFE0 100%);
}}
#npi-hotspot-panel .npi-hotspot-content {{
    padding: 9px;
    background: linear-gradient(145deg, #FFFFFF 0%, #F8FBFF 100%);
}}
#npi-hotspot-panel .npi-hotspot-row {{
    min-height: 36px;
    margin-bottom: 6px;
    padding: 7px 9px;
    border: 1px solid #E0E8F2;
    border-radius: 7px;
    background: #FFFFFF;
    color: #263A54;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: 700;
    transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease;
}}
#npi-hotspot-panel .npi-hotspot-row:hover {{
    background: #F7FAFE;
    border-color: #B9CDE4;
    box-shadow: 0 3px 8px rgba(31, 67, 111, 0.08);
    transform: translateY(-1px);
}}
#npi-hotspot-panel .npi-hotspot-row:focus-visible {{
    outline: 2px solid #376A9E;
    outline-offset: 1px;
}}
#npi-hotspot-panel .npi-hotspot-operation {{
    color: #2B405A;
    font-weight: 700;
}}
#npi-hotspot-panel .npi-hotspot-count {{
    justify-self: end;
    min-width: 36px;
    padding: 3px 7px;
    border: 1px solid #DDE8F5;
    border-radius: 999px;
    background: #EDF4FC;
    color: #315C8B;
    font-size: 10px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
}}
#npi-hotspot-panel .npi-hotspot-content::-webkit-scrollbar {{
    width: 7px;
}}
#npi-hotspot-panel .npi-hotspot-content::-webkit-scrollbar-thumb {{
    background: #D8DEE8;
    border-radius: 999px;
}}

/* ============================================================
   EXECUTIVE STATUS — MODERN VISUAL TREATMENT
   Scoped to Executive Status; no changes to other widget styles.
   ============================================================ */
#npi-executive-panel {{
    border: 1px solid #D8E2EF !important;
    border-radius: 12px !important;
    background: #FFFFFF !important;
    box-shadow: 0 8px 22px rgba(27, 49, 79, 0.10) !important;
}}
#npi-executive-panel .npi-floating-header {{
    height: 38px;
    min-height: 38px;
    padding: 0 12px;
    gap: 9px;
    background: linear-gradient(90deg, #EEF4FC 0%, #F8FAFD 100%);
    border-bottom: 1px solid #DCE6F2;
    color: #183153;
}}
#npi-executive-panel .npi-floating-header::before {{
    content: "";
    display: block;
    flex: 0 0 4px;
    width: 4px;
    height: 20px;
    border-radius: 999px;
    background: linear-gradient(180deg, #2F80ED 0%, #5B9CF0 100%);
}}
.npi-executive-heading {{
    display: flex;
    align-items: baseline;
    gap: 9px;
    min-width: 0;
    flex: 1 1 auto;
    white-space: nowrap;
}}
.npi-executive-title {{
    color: #183153;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.9px;
    line-height: 1.15;
}}
.npi-executive-subtitle {{
    color: #708198;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 9px;
    font-weight: 500;
    letter-spacing: 0.05px;
    line-height: 1.2;
}}
#npi-executive-panel .npi-floating-header-actions {{
    flex: 0 0 auto;
    gap: 6px;
}}
#npi-executive-panel .npi-floating-reset {{
    height: 24px;
    padding: 0 9px;
    border: 1px solid #C7D5E7;
    border-radius: 6px;
    background: #FFFFFF;
    color: #355477;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0.2px;
}}
#npi-executive-panel .npi-floating-reset:hover {{
    background: #F7FAFE;
    border-color: #8FB2DF;
}}
#npi-executive-panel .npi-executive-floating-content {{
    height: calc(100% - 38px);
    padding: 8px 10px;
    background: linear-gradient(145deg, #FFFFFF 0%, #F8FBFF 100%);
}}
#npi-executive-panel .npi-executive-floating-inner {{
    gap: 10px;
    align-items: center;
}}
/* Keep the SVG itself as the only donut visual: no extra circle/shadow behind it. */
#npi-executive-panel .npi-floating-donut {{
    flex: 0 0 260px;
    width: 260px;
    height: 260px;
    border: none;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
}}
#npi-executive-panel .npi-float-legend {{
    border: 1px solid #E0E8F2;
    border-radius: 7px;
    background: #FFFFFF;
    color: #263A54;
    font-family: Arial, Helvetica, sans-serif;
    box-shadow: 0 1px 2px rgba(25, 48, 78, 0.035);
    transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease;
}}
#npi-executive-panel .npi-float-legend:hover {{
    background: #FFFFFF;
    border-color: #B7CBE3;
    box-shadow: 0 3px 8px rgba(31, 67, 111, 0.08);
    transform: translateY(-1px);
}}
#npi-executive-panel .npi-float-count {{
    color: #182D49;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
}}
#npi-executive-panel .npi-float-percent {{
    color: #728299;
    font-variant-numeric: tabular-nums;
}}

/* ============================================================
   DELAY COMMITMENT — HIGH-PRIORITY STATUS TILES
   Visual-only update. Existing tile IDs/data attributes and
   click/drag/resize controller remain unchanged.
   ============================================================ */
#npi-delay-commitment-panel {{
    border: 1px solid #D5AEB2 !important;
    border-radius: 13px !important;
    background: #FFFFFF !important;
    box-shadow: 0 10px 26px rgba(82, 28, 37, 0.16), 0 2px 6px rgba(27, 49, 79, 0.06) !important;
}}
#npi-delay-commitment-panel .npi-floating-header {{
    height: 38px;
    min-height: 38px;
    padding: 0 12px;
    gap: 9px;
    background: linear-gradient(100deg, #17283F 0%, #263F5D 72%, #334D6C 100%);
    border-bottom: 2px solid #D94F5C;
    color: #FFFFFF;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    font-weight: 900;
    letter-spacing: 1px;
}}
#npi-delay-commitment-panel .npi-floating-header::before {{
    content: "";
    display: block;
    flex: 0 0 5px;
    width: 5px;
    height: 22px;
    border-radius: 999px;
    background: linear-gradient(180deg, #FF8A8A 0%, #DC3545 100%);
    box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.08);
}}
#npi-delay-commitment-panel .npi-commitment-priority-badge {{
    display: inline-flex;
    align-items: center;
    margin-left: auto;
    padding: 4px 8px;
    border: 1px solid rgba(255, 255, 255, 0.24);
    border-radius: 999px;
    background: #D94F5C;
    color: #FFFFFF;
    font-size: 9px;
    line-height: 1;
    font-weight: 900;
    letter-spacing: 0.8px;
    white-space: nowrap;
}}
#npi-delay-commitment-panel .npi-delay-commitment-content {{
    width: 100%;
    height: calc(100% - 38px);
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    grid-template-rows: minmax(0, 1fr);
    gap: 7px;
    padding: 8px;
    overflow: hidden;
    background: linear-gradient(145deg, #FFFFFF 0%, #F5F7FB 100%);
}}
#npi-delay-commitment-panel .npi-delay-commitment-tile {{
    position: relative;
    width: 100%;
    min-width: 0;
    min-height: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: center;
    gap: 3px;
    padding: 8px 10px 7px 12px;
    border: 1px solid var(--commitment-border, #D7DADF);
    border-left: 4px solid var(--commitment-accent, #D94F5C);
    border-radius: 10px;
    background: linear-gradient(145deg, var(--commitment-bg, #FFFFFF) 0%, #FFFFFF 100%);
    color: #25364B;
    text-align: left;
    cursor: pointer;
    font-family: Arial, Helvetica, sans-serif;
    box-shadow: 0 2px 6px rgba(34, 52, 78, 0.055);
    transition: transform 130ms ease, box-shadow 130ms ease, filter 130ms ease;
}}
#npi-delay-commitment-panel .npi-delay-commitment-tile:hover {{
    transform: translateY(-2px);
    filter: saturate(1.04) brightness(0.995);
    box-shadow: 0 7px 15px rgba(32, 54, 78, 0.14);
}}
#npi-delay-commitment-panel .npi-delay-commitment-tile:focus-visible {{
    outline: 3px solid #274C75;
    outline-offset: 1px;
}}
#npi-delay-commitment-panel .npi-delay-commitment-tile.is-selected {{
    border: 2px solid var(--commitment-accent, #D94F5C);
    border-left-width: 5px;
    box-shadow: 0 0 0 2px rgba(217, 79, 92, 0.13), 0 5px 12px rgba(32, 54, 78, 0.12);
}}
#npi-delay-commitment-panel .npi-commitment-icon {{
    position: absolute;
    top: 7px;
    right: 7px;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.82);
    color: var(--commitment-accent, #D94F5C);
    font-size: 14px;
    line-height: 1;
    font-weight: 900;
}}
#npi-delay-commitment-panel .npi-commitment-label {{
    display: block;
    width: 100%;
    padding-right: 27px;
    color: var(--commitment-accent, #3B4A5D);
    font-size: 12px;
    line-height: 1.15;
    font-weight: 900;
    letter-spacing: 0.15px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
#npi-delay-commitment-panel .npi-commitment-count {{
    display: block;
    color: #17283F;
    font-size: 29px;
    line-height: 0.98;
    font-weight: 900;
    letter-spacing: -0.65px;
    font-variant-numeric: tabular-nums;
}}
#npi-delay-commitment-panel .npi-commitment-unit {{
    display: block;
    color: #69788D;
    font-size: 9px;
    line-height: 1.1;
    font-weight: 800;
    letter-spacing: 1px;
}}
#npi-delay-commitment-panel .npi-delay-commitment-content::-webkit-scrollbar {{
    width: 7px;
}}
#npi-delay-commitment-panel .npi-delay-commitment-content::-webkit-scrollbar-thumb {{
    background: #D8E0EB;
    border-radius: 999px;
}}

/* Standardized layout V5. Geometry is shared with the native Enquiry form. */
.npi-floating-stage {{ height:624px; overflow:hidden; }}
#npi-dashboard-scroll {{ position:absolute; inset:0 0 18px; overflow:auto; scrollbar-gutter:stable; overscroll-behavior:auto; }}
#npi-layout-canvas {{ position:relative; width:100%; min-height:1px; }}
.npi-floating-panel {{ min-width:0; min-height:120px; border:1px solid #d8e2ed; border-radius:12px; box-shadow:0 3px 10px rgba(25,48,76,.07); }}
.npi-enquiry-slot {{ position:absolute; pointer-events:none; }}
.npi-floating-header {{ height:38px; padding:0 12px; background:#20354f; color:#fff; font:800 12px Arial,Helvetica,sans-serif; letter-spacing:.35px; border-bottom:1px solid #d8e2ed; }}
.npi-executive-subtitle {{ color:#d6e3ef; font-size:10px; }}
.npi-floating-reset {{ font-size:9px; padding:4px 6px; }}
.npi-floating-legend {{ flex:1 1 0; width:0; min-width:0; }}
.npi-float-legend {{ font-family:Arial,Helvetica,sans-serif; font-size:11px; }}
.npi-float-label {{ flex:1 1 auto; min-width:0; white-space:normal; margin-right:6px; overflow-wrap:anywhere; }}
.npi-floating-donut svg {{ display:block; width:100%; height:100%; }}
.npi-executive-floating-content {{ overflow:auto; }}
#npi-delay-commitment-panel {{ border-color:#d5aeb2; }}
#npi-delay-commitment-panel .npi-commitment-count {{ font-size:28px; }}
#npi-delay-bucket-panel .npi-delay-bucket-label {{ font-size:10px; }}
#npi-delay-bucket-panel .npi-delay-bucket-count {{ font-size:24px; }}
#npi-hotspot-panel .npi-hotspot-row {{ min-height:34px; }}
#npi-hotspot-panel .npi-hotspot-operation {{ white-space:normal; overflow-wrap:anywhere; }}
.npi-floating-stage:not([data-layout-mode="desktop"]) .npi-floating-panel {{ resize:none !important; }}
.npi-floating-stage:not([data-layout-mode="desktop"]) .npi-floating-header {{ cursor:default; }}
.npi-floating-stage:not([data-layout-mode="desktop"]) .npi-executive-floating-inner {{ flex-direction:column; gap:10px; }}
.npi-floating-stage:not([data-layout-mode="desktop"]) .npi-floating-legend {{ flex:0 0 auto; width:100%; }}
.npi-floating-stage:not([data-layout-mode="desktop"]) .npi-floating-panel button {{ min-height:38px; }}
.npi-floating-stage[data-layout-mode="mobile"] #npi-delay-commitment-panel .npi-delay-commitment-content {{ grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-rows:repeat(2,minmax(0,1fr)); }}
.npi-floating-stage[data-layout-mode="mobile"] .npi-floating-header {{ font-size:11px; }}
.npi-floating-stage[data-layout-mode="mobile"] .npi-floating-panel button {{ min-height:42px; touch-action:manipulation; }}
.npi-floating-stage[data-layout-mode="mobile"] .npi-float-legend {{ min-height:38px; }}


@media(pointer:coarse) {{
 .npi-floating-panel {{ resize:none !important; touch-action:pan-x pan-y; }}
 .npi-floating-header {{ cursor:default !important; }}
}}

</style>


<div
    class="npi-floating-stage"
    id="npi-floating-stage"
    data-layout-owner="{dashboard_layout_owner}"
>
<div id="npi-dashboard-scroll"><div id="npi-layout-canvas">

    <!-- ========================================================
         PART DELAY DAYS BUCKET
         ======================================================== -->

    <div
        class="npi-floating-panel"
        id="npi-delay-bucket-panel"
        data-layout-id="delayBucket"
        style="left:12px; top:200px; width:310px; height:292px;"
    >
        <div class="npi-floating-header" data-drag-handle>
            <span>PART DELAY DAYS BUCKET</span>
        </div>
        <div class="npi-delay-bucket-content">
            {bucket_tiles_html}
        </div>
    </div>

    <!-- ========================================================
         PRODUCTION HOTSPOT
         ======================================================== -->
    <div class="npi-floating-panel" id="npi-hotspot-panel" data-layout-id="hotspot" style="left:970px; top:12px; width:fit-content; min-width:0; height:300px;">
        <div class="npi-floating-header" data-drag-handle style="padding-left:10px;padding-right:10px;white-space:nowrap;"><span>PRODUCTION HOTSPOT</span></div>
        <div class="npi-hotspot-content">{hotspot_rows_html if hotspot_rows_html else '<div style="padding:8px;color:#777;font-size:12px;">No production hotspot found.</div>'}</div>
    </div>

    <!-- ========================================================
         EXECUTIVE STATUS
         ======================================================== -->

    <div
        class="npi-floating-panel"
        id="npi-executive-panel"
        data-layout-id="executive"
        style="
            left:300px;
            top:12px;
            width:650px;
            height:370px;
        "
    >

        <div
            class="npi-floating-header"
            data-drag-handle
        >

            <div class="npi-executive-heading">
                <span class="npi-executive-title">EXECUTIVE STATUS</span>
                <span class="npi-executive-subtitle">Portfolio status at a glance</span>
            </div>


            <div
                class="npi-floating-header-actions"
            >

                <button
                    type="button"
                    class="npi-floating-reset"
                    id="npi-reset-layout"
                >
                    RESET LAYOUT
                </button>

            </div>

        </div>


        <div
            class="npi-executive-floating-content"
        >

            <div
                class="npi-executive-floating-inner"
            >

                <div
                    class="npi-floating-donut"
                >

                    {donut_svg}

                </div>


                <div
                    class="npi-floating-legend"
                >

                    {legend_html}

                </div>

            </div>

        </div>

    </div>

    <!-- ========================================================
         DELAY COMMITMENT
         ======================================================== -->
    <div class="npi-floating-panel" id="npi-delay-commitment-panel" data-layout-id="delayCommitment"
         style="left:330px;top:395px;width:760px;height:140px;">
        <div class="npi-floating-header" data-drag-handle>
            <span>DELAY COMMITMENT</span>
            <span class="npi-commitment-priority-badge">HIGH PRIORITY</span>
        </div>
        <div class="npi-delay-commitment-content">
            {commitment_tiles_html}
        </div>
    </div>

    <div class="npi-enquiry-slot" data-layout-id="enquiry" aria-hidden="true"></div>
</div></div>
    <div id="npi-dashboard-drilldown-boundary"
         class="npi-dashboard-drilldown-boundary"
         role="separator"
         aria-orientation="horizontal"
         aria-label="Drag to adjust the space before Drilldown"
         title="Drag up/down to adjust the space before Drilldown">
        <span class="npi-dashboard-drilldown-boundary-grip"></span>
    </div>

</div>


<script>

(function () {{

    /* ========================================================
       STORAGE
       ======================================================== */

    // LAYOUT V5: geometry and preferences only; widget callbacks stay unchanged.
    const LAYOUT_OWNER = {json.dumps(dashboard_layout_owner)};
    const LAYOUT_VERSION = 5;
    const PANEL_IDS = ['delayCommitment','enquiry','executive','delayBucket','hotspot'];
    let currentMode = null;
    let currentPreferences = null;
    let currentLayout = null;
    let gestureDepth = 0;
    let resizeTimer = null;
    function getParentDocument(){{ try{{return window.parent.document;}}catch(e){{return null;}} }}
    function clamp(value,minimum,maximum){{return Math.max(minimum,Math.min(maximum,value));}}
    function layoutMode(width){{return width>=1200?'desktop':width>760?'tablet':'mobile';}}
    function preferenceKey(mode){{return 'npi_dashboard_v5:'+LAYOUT_OWNER+':'+mode;}}
    function defaultLayout(width){{
        const mode=layoutMode(width), pad=12, gap=12, inner=Math.max(1,width-pad*2);
        const rects={{}};
        function put(id,left,top,w,h){{rects[id]={{left,top,width:w,height:h}};}}
        if(mode==='desktop'){{
            const enquiry=320, bucket=240, hotspot=Math.max(320,inner*0.285);
            put('delayCommitment',pad,pad,inner-enquiry-gap,220);
            put('enquiry',width-pad-enquiry,pad,enquiry,220);
            put('executive',pad,244,inner-bucket-hotspot-gap*2,350);
            put('delayBucket',rects.executive.left+rects.executive.width+gap,244,bucket,350);
            put('hotspot',width-pad-hotspot,244,hotspot,350);
        }}else if(mode==='tablet'){{
            const col=(inner-gap)/2;
            put('delayCommitment',pad,pad,inner,180);
            put('executive',pad,204,col,490);
            put('hotspot',pad+col+gap,204,col,490);
            put('delayBucket',pad,706,col,350);
            put('enquiry',pad+col+gap,706,col,350);
        }}else{{
            const heights={{delayCommitment:280,executive:510,hotspot:320,delayBucket:350,enquiry:230}};
            let top=pad;
            ['delayCommitment','executive','hotspot','delayBucket','enquiry'].forEach(function(id){{
                put(id,pad,top,inner,heights[id]);top+=heights[id]+gap;
            }});
        }}
        const height=Math.max(...Object.values(rects).map(r=>r.top+r.height))+pad;
        return {{mode,width,rects,height}};
    }}
    function overlaps(a,b){{return a.left<b.left+b.width && b.left<a.left+a.width && a.top<b.top+b.height && b.top<a.top+a.height;}}
    function validRects(rects,width){{
        if(!rects || !PANEL_IDS.every(id=>rects[id]))return false;
        return PANEL_IDS.every(function(id,index){{
            const r=rects[id];
            return ['left','top','width','height'].every(k=>Number.isFinite(r[k])) &&
                r.left>=0 && r.top>=0 && r.width>=Math.min(180,width-24) && r.height>=120 &&
                r.left+r.width<=width+1 && r.top+r.height<=5000 &&
                PANEL_IDS.slice(index+1).every(other=>!overlaps(r,rects[other]));
        }});
    }}
    function readPreferences(mode){{
        try{{
            const stored=JSON.parse(window.parent.localStorage.getItem(preferenceKey(mode))||'null');
            if(stored && stored.version===LAYOUT_VERSION && stored.owner===LAYOUT_OWNER && stored.mode===mode && stored.custom===true)return stored;
        }}catch(e){{}}
        return null;
    }}
    function persistPreferences(){{
        if(!currentPreferences || !currentPreferences.custom)return;
        try{{window.parent.localStorage.setItem(preferenceKey(currentMode),JSON.stringify(currentPreferences));}}catch(e){{}}
    }}
    function ensureCustom(){{
        if(!currentPreferences)currentPreferences={{version:LAYOUT_VERSION,owner:LAYOUT_OWNER,mode:currentMode,custom:true}};
        currentPreferences.custom=true;
    }}
    function publishLayout(){{
        window.dispatchEvent(new Event('npi-dashboard-layout'));
    }}
    function panelFor(id){{return document.querySelector('[data-layout-id="'+id+'"]');}}
    function paintLayout(){{
        if(!currentLayout)return;
        const canvas=document.getElementById('npi-layout-canvas');
        canvas.style.height=currentLayout.height+'px';
        PANEL_IDS.forEach(function(id){{
            const panel=panelFor(id), r=currentLayout.rects[id];if(!panel)return;
            Object.keys(r).forEach(key=>panel.style.setProperty(key,r[key]+'px'));
            panel.style.resize=currentMode==='desktop' && id!=='enquiry'?'both':'none';
        }});
        const executive=panelFor('executive');
        const donut=executive && executive.querySelector('.npi-floating-donut');
        if(donut){{
            const size=currentMode==='desktop'?Math.min(260,Math.max(180,currentLayout.rects.executive.width*0.38)):210;
            donut.style.width=size+'px';donut.style.height=size+'px';donut.style.flexBasis=size+'px';
        }}
        document.getElementById('npi-floating-stage').dataset.layoutMode=currentMode;
        publishLayout();
    }}
    function reflowLayout(){{
        const scroll=document.getElementById('npi-dashboard-scroll');
        const width=Math.max(1,scroll.clientWidth);
        const base=defaultLayout(width), changed=base.mode!==currentMode;
        if(changed){{currentMode=base.mode;currentPreferences=readPreferences(currentMode);}}
        currentLayout=base;
        const saved=currentPreferences && currentPreferences.widgets;
        // Absolute custom geometry is restored only at the width where the user chose it.
        // At another width use readable defaults; retain the saved preference for return.
        if(saved && Math.abs(Number(currentPreferences.width)-width)<=2 && validRects(saved,width)){{
            currentLayout.rects=JSON.parse(JSON.stringify(saved));
            currentLayout.height=Math.max(...Object.values(saved).map(r=>r.top+r.height))+12;
        }}
        DEFAULT_DASHBOARD_HEIGHT=base.height+18;
        paintLayout();
        applyDashboardHeight(readDashboardHeight(),false);
    }}
    function loadLayout(){{reflowLayout();}}
    function saveLayout(){{
        // Called only on completion of an explicit user gesture.
        ensureCustom();
        currentPreferences.width=currentLayout.width;
        currentPreferences.widgets=JSON.parse(JSON.stringify(currentLayout.rects));
        persistPreferences();
    }}
    function saveDashboardHeight(height){{
        ensureCustom();currentPreferences.dashboardHeight=height;persistPreferences();
    }}
    function updatePanel(id,patch,save){{
        if(currentMode!=='desktop' || !currentLayout.rects[id])return false;
        const rects=JSON.parse(JSON.stringify(currentLayout.rects));
        Object.assign(rects[id],patch);
        if(!validRects(rects,currentLayout.width)){{paintLayout();return false;}}
        currentLayout.rects=rects;
        currentLayout.height=Math.max(...Object.values(rects).map(r=>r.top+r.height))+12;
        paintLayout();if(save)saveLayout();return true;
    }}
    function resetLayout(){{
        // Reset every viewport preference for this user only. Legacy data is untouched.
        ['desktop','tablet','mobile'].forEach(function(mode){{
            try{{window.parent.localStorage.removeItem(preferenceKey(mode));}}catch(e){{}}
        }});
        currentPreferences=null;currentMode=null;reflowLayout();
    }}
    function enableDragging(panel){{
        const handle=panel.querySelector('[data-drag-handle]');if(!handle)return;
        let drag=null;
        handle.addEventListener('pointerdown',function(event){{
            if(currentMode!=='desktop' || event.pointerType!=='mouse' || event.button!==0 || event.target.closest('button'))return;
            const r=currentLayout.rects[panel.dataset.layoutId];
            drag={{x:event.clientX,y:event.clientY,left:r.left,top:r.top}};gestureDepth++;
            try{{handle.setPointerCapture(event.pointerId);}}catch(e){{}}event.preventDefault();
        }});
        handle.addEventListener('pointermove',function(event){{
            if(!drag)return;
            updatePanel(panel.dataset.layoutId,{{left:Math.max(0,drag.left+event.clientX-drag.x),top:Math.max(0,drag.top+event.clientY-drag.y)}},false);
        }});
        function finish(){{if(!drag)return;drag=null;gestureDepth=Math.max(0,gestureDepth-1);saveLayout();}}
        handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);
    }}
    function enableResizeSaving(panel){{
        let resizing=false;
        panel.addEventListener('pointerdown',function(event){{
            if(currentMode!=='desktop' || event.pointerType!=='mouse')return;
            const r=panel.getBoundingClientRect();
            if(event.clientX>=r.right-18 && event.clientY>=r.bottom-18){{resizing=true;gestureDepth++;}}
        }});
        function finish(){{
            if(!resizing)return;resizing=false;gestureDepth=Math.max(0,gestureDepth-1);
            updatePanel(panel.dataset.layoutId,{{width:panel.offsetWidth,height:panel.offsetHeight}},true);
        }}
        document.addEventListener('pointerup',finish);document.addEventListener('pointercancel',finish);
    }}
    window.__npiDashboardLayoutV5={{owner:LAYOUT_OWNER,getLayout:()=>currentLayout,updatePanel,resetLayout,
        beginGesture:()=>gestureDepth++,endGesture:()=>{{gestureDepth=Math.max(0,gestureDepth-1);}},sync:publishLayout}};
    function queueResponsiveReflow(){{
        clearTimeout(resizeTimer);resizeTimer=setTimeout(function(){{
            const scroll=document.getElementById('npi-dashboard-scroll');
            if(!gestureDepth && scroll && currentLayout && Math.abs(scroll.clientWidth-currentLayout.width)>1)reflowLayout();
            else publishLayout();
        }},100);
    }}
    window.addEventListener('resize',queueResponsiveReflow);
    window.addEventListener('orientationchange',queueResponsiveReflow);
    let viewportSource=null;
    try{{viewportSource=window.parent.visualViewport;if(viewportSource)viewportSource.addEventListener('resize',queueResponsiveReflow);}}catch(e){{}}
    window.addEventListener('pagehide',function(){{
        clearTimeout(resizeTimer);if(viewportSource)viewportSource.removeEventListener('resize',queueResponsiveReflow);
    }},{{once:true}});

    /* ========================================================
       FIND STREAMLIT BRIDGE BUTTON
       ======================================================== */

    function findBridgeButton(
        classification,
        label
    ) {{

        const parentDoc =
            getParentDocument();


        if (!parentDoc) {{
            return null;
        }}


        try {{

            const buttons =
                Array.from(
                    parentDoc.querySelectorAll(
                        "button"
                    )
                );


            /*
             * First attempt:
             * exact visible button text.
             *
             * This is deliberately checked first because
             * the bridge buttons are real Streamlit buttons.
             */

            let target =
                buttons.find(
                    function(button) {{

                        const text =
                            (
                                button.innerText
                                || ""
                            )
                            .replace(
                                /\u00A0/g,
                                " "
                            )
                            .trim();


                        return (
                            text
                            ===
                            label
                        );

                    }}
                );


            if (target) {{
                return target;
            }}


            /*
             * Second attempt:
             * button starts with label.
             */

            target =
                buttons.find(
                    function(button) {{

                        const text =
                            (
                                button.innerText
                                || ""
                            )
                            .replace(
                                /\u00A0/g,
                                " "
                            )
                            .trim();


                        return text
                            .startsWith(
                                label
                            );

                    }}
                );


            if (target) {{
                return target;
            }}


            /*
             * Third attempt:
             * classification marker, if available.
             */

            target =
                buttons.find(
                    function(button) {{

                        return (
                            button.getAttribute(
                                "data-npi-classification"
                            )
                            ===
                            classification
                        );

                    }}
                );


            return target || null;

        }}
        catch (error) {{

            return null;

        }}

    }}


    /* ========================================================
       HIDE STREAMLIT BRIDGE BUTTONS
       ======================================================== */

    function hideBridgeButtons() {{

        const parentDoc =
            getParentDocument();


        if (!parentDoc) {{
            return;
        }}


        try {{

            const buttons =
                Array.from(
                    parentDoc.querySelectorAll(
                        "button"
                    )
                );


            const bridgeLabels = [
                "ALL",
                "ENGINEERING",
                "PROCESS",
                "PRODUCTION",
                "ON TRACK",
                "COMPLETE",
                "<30 DAYS",
                "30–90 DAYS",
                "3–6 MONTHS",
                "6–12 MONTHS",
                "1–2 YEARS",
                ">2 YEARS",
                "DELAY COMMITMENT BRIDGE OVERDUE",
                "DELAY COMMITMENT BRIDGE DUE TODAY",
                "DELAY COMMITMENT BRIDGE UPCOMING",
                "DELAY COMMITMENT BRIDGE NO COMMITMENT"
            ];


            buttons.forEach(
                function(button) {{

                    const text =
                        (
                            button.innerText
                            || ""
                        )
                        .replace(
                            /\u00A0/g,
                            " "
                        )
                        .trim();


                    const isBridge =
                        bridgeLabels.some(
                            function(label) {{

                                return (
                                    text
                                    ===
                                    label
                                    ||
                                    text.startsWith(
                                        label + " "
                                    )
                                );

                            }}
                        );


                    if (!isBridge) {{
                        return;
                    }}


                    let element =
                        button;


                    for (
                        let level = 0;
                        level < 5 && element;
                        level++
                    ) {{

                        const tag =
                            (
                                element.tagName
                                || ""
                            ).toLowerCase();


                        if (
                            tag
                            ===
                            "div"
                            &&
                            (
                                element.hasAttribute(
                                    "data-testid"
                                )
                                ||
                                element.className
                            )
                        ) {{

                            const rect =
                                element.getBoundingClientRect();


                            if (
                                rect.width
                                >
                                0
                                &&
                                rect.height
                                >
                                0
                            ) {{

                                const buttonRect =
                                    button.getBoundingClientRect();


                                if (
                                    rect.width
                                    >=
                                    buttonRect.width
                                    &&
                                    rect.height
                                    >=
                                    buttonRect.height
                                ) {{

                                    element.style.setProperty(
                                        "display",
                                        "none",
                                        "important"
                                    );

                                    break;

                                }}

                            }}

                        }}


                        element =
                            element.parentElement;

                    }}

                }}
            );

        }}
        catch (error) {{}}

    }}


    /* ========================================================
       CLICK STREAMLIT BRIDGE
       ======================================================== */

    function clickStreamlitLegend(
        classification,
        label
    ) {{

        const target =
            findBridgeButton(
                classification,
                label
            );


        if (!target) {{
            return;
        }}


        try {{

            /*
             * Real Streamlit button click.
             *
             * Its Python on_click callback will set:
             *
             * st.session_state.selected_classification
             *
             * and Streamlit will rerun.
             */

            target.click();

        }}
        catch (error) {{}}

    }}


    /* ========================================================
       LEGEND CLICK
       ======================================================== */

    function wireLegend() {{

        document
            .querySelectorAll(
                ".npi-float-legend"
            )
            .forEach(
                function(button) {{

                    button.addEventListener(
                        "click",
                        function(event) {{

                            event.preventDefault();

                            event.stopPropagation();


                            const classification =
                                button.getAttribute(
                                    "data-classification"
                                );


                            const label =
                                button.getAttribute(
                                    "data-label"
                                );


                            clickStreamlitLegend(
                                classification,
                                label
                            );

                        }}
                    );

                }}
            );

    }}


    /* ========================================================
       DONUT CENTER -> ALL BRIDGE
       ======================================================== */

    function wireDonutCenter() {{

        document
            .querySelectorAll('a[aria-label="ALL UNIQUE PARTS"]')
            .forEach(
                function(centerLink) {{

                    if (centerLink.dataset.npiAllWired === "1") {{
                        return;
                    }}

                    centerLink.dataset.npiAllWired = "1";

                    centerLink.addEventListener(
                        "click",
                        function(event) {{

                            // Keep the click inside this iframe; do not navigate
                            // the parent Streamlit page through the SVG href.
                            event.preventDefault();
                            event.stopPropagation();

                            clickStreamlitLegend(
                                "ALL",
                                "ALL"
                            );

                        }}
                    );

                }}
            );

    }}


    /* ========================================================
       DELAY BUCKET TILE CLICK
       ======================================================== */

    function wireDelayBucketTiles() {{
        document.querySelectorAll(".npi-delay-bucket-tile").forEach(function(tile) {{
            tile.addEventListener("click", function(event) {{
                event.preventDefault();
                event.stopPropagation();
                const bucket = tile.getAttribute("data-bucket");
                const parentDoc = getParentDocument();
                if (!parentDoc || !bucket) return;
                const target = Array.from(parentDoc.querySelectorAll("button")).find(function(button) {{
                    return (button.innerText || "").replace(/\u00A0/g, " ").trim() === bucket;
                }});
                if (target) target.click();
            }});
        }});
    }}

    /* ========================================================
       PRODUCTION HOTSPOT ROW CLICK
       ======================================================== */
    function wireHotspotRows() {{
        document.querySelectorAll(".npi-hotspot-row").forEach(function(row) {{
            row.addEventListener("click", function(event) {{
                event.preventDefault();
                event.stopPropagation();
                const operation = row.getAttribute("data-hotspot");
                const parentDoc = getParentDocument();
                if (!parentDoc || !operation) return;
                const target = Array.from(parentDoc.querySelectorAll("button")).find(function(button) {{
                    return (button.innerText || "").replace(/\u00A0/g, " ").trim() === operation;
                }});
                if (target) target.click();
            }});
        }});
    }}

    /* ========================================================
       DELAY COMMITMENT TILE CLICK
       ======================================================== */
    function wireDelayCommitmentTiles() {{
        document.querySelectorAll(".npi-delay-commitment-tile").forEach(function(tile) {{
            if (tile.dataset.npiCommitmentWired === "1") return;
            tile.dataset.npiCommitmentWired = "1";
            tile.addEventListener("click", function(event) {{
                event.preventDefault();
                event.stopPropagation();
                const bridgeLabel = tile.getAttribute("data-bridge-label");
                if (!bridgeLabel) return;
                clickStreamlitLegend("DELAY_COMMITMENT", bridgeLabel);
            }});
        }});
    }}

    /* ========================================================
       RESIZABLE DASHBOARD / DRILLDOWN BOUNDARY
       ======================================================== */

    let DEFAULT_DASHBOARD_HEIGHT = 624;
    const MAX_DASHBOARD_HEIGHT = 900;
    function getDashboardMinimumHeight() {{ return 300; }}
    function readDashboardHeight() {{
        const height=currentPreferences && currentPreferences.dashboardHeight;
        return Number.isFinite(height) ? height : DEFAULT_DASHBOARD_HEIGHT;
    }}

    function syncDrilldownGap() {{
        // Keep the native Drilldown anchor exactly 20px below the dashboard iframe.
        // Streamlit renders several wrapper elements around components, so measuring
        // the real DOM positions is more reliable than guessing their default margins.
        try {{
            if (window.parent.__npiTouchActive) return;
            const frame = window.frameElement;
            const anchor = window.parent.document.getElementById("npi-drilldown-anchor");
            if (!frame || !anchor) return;

            let drilldownBlock = anchor;
            while (drilldownBlock && drilldownBlock !== window.parent.document.body) {{
                if (drilldownBlock.getAttribute && drilldownBlock.getAttribute("data-testid") === "stElementContainer") break;
                drilldownBlock = drilldownBlock.parentElement;
            }}
            if (!drilldownBlock || drilldownBlock === window.parent.document.body) return;

            const targetGap = 20;
            const frameBottom = frame.getBoundingClientRect().bottom;
            const anchorTop = anchor.getBoundingClientRect().top;
            const correction = frameBottom + targetGap - anchorTop;
            const currentMargin = parseFloat(drilldownBlock.style.marginTop || "0") || 0;
            if (Math.abs(correction) > 0.5) {{
                drilldownBlock.style.setProperty("margin-top", (currentMargin + correction) + "px", "important");
            }}
        }} catch (error) {{}}
    }}

    function installDrilldownGapObserver() {{
        try {{
            const parentWin = window.parent;
            const parentDoc = parentWin.document;
            const KEY = "__npiDashboardDrilldownGapControllerV1";
            let controller = parentWin[KEY];
            if (!controller) {{
                controller = {{ observer: null, timer: null, sync: null }};
                parentWin[KEY] = controller;
            }}
            controller.sync = syncDrilldownGap;
            if (!controller.observer) {{
                controller.observer = new parentWin.MutationObserver(function () {{
                    if (controller.timer) parentWin.clearTimeout(controller.timer);
                    controller.timer = parentWin.setTimeout(function () {{
                        try {{ if (controller.sync) controller.sync(); }} catch (error) {{}}
                    }}, 60);
                }});
                controller.observer.observe(parentDoc.body, {{ childList: true, subtree: true }});
            }}
            [0, 100, 300, 700, 1200].forEach(function (delay) {{
                parentWin.setTimeout(function () {{
                    try {{ if (controller.sync) controller.sync(); }} catch (error) {{}}
                }}, delay);
            }});
        }} catch (error) {{}}
    }}

    function applyDashboardHeight(requestedHeight, persist) {{
        const stage = document.getElementById("npi-floating-stage");
        if (!stage) return DEFAULT_DASHBOARD_HEIGHT;
        const minimum = getDashboardMinimumHeight();
        const numeric = Number(requestedHeight);
        const wanted = Number.isFinite(numeric) ? numeric : DEFAULT_DASHBOARD_HEIGHT;
        const height = clamp(wanted, minimum, Math.max(MAX_DASHBOARD_HEIGHT, DEFAULT_DASHBOARD_HEIGHT));
        stage.style.setProperty("height", height + "px", "important");
        stage.style.setProperty("min-height", height + "px", "important");

        // Resizing only the iframe's inner HTML does not change the reserved
        // Streamlit layout height. Update the iframe, its immediate host, and the
        // owning Streamlit element container so native Drilldown reflows with it.
        try {{
            const frame = window.frameElement;
            if (frame) {{
                const pxHeight = height + "px";
                ["height", "min-height", "max-height"].forEach(function (property) {{
                    frame.style.setProperty(property, pxHeight, "important");
                }});
                frame.style.setProperty("display", "block", "important");
                frame.style.setProperty("box-sizing", "border-box", "important");

                const frameHost = frame.parentElement;
                if (frameHost && frameHost !== window.parent.document.body) {{
                    ["height", "min-height", "max-height"].forEach(function (property) {{
                        frameHost.style.setProperty(property, pxHeight, "important");
                    }});
                    frameHost.style.setProperty("box-sizing", "border-box", "important");
                    frameHost.style.setProperty("overflow", "visible", "important");
                }}

                let elementContainer = frame;
                while (elementContainer && elementContainer !== window.parent.document.body) {{
                    if (elementContainer.getAttribute && elementContainer.getAttribute("data-testid") === "stElementContainer") break;
                    elementContainer = elementContainer.parentElement;
                }}
                if (elementContainer && elementContainer !== window.parent.document.body) {{
                    ["height", "min-height", "max-height"].forEach(function (property) {{
                        elementContainer.style.setProperty(property, pxHeight, "important");
                    }});
                    elementContainer.style.setProperty("box-sizing", "border-box", "important");
                    elementContainer.style.setProperty("margin-top", "0px", "important");
                    // The regular Streamlit vertical gap is 16px; 4px bottom margin
                    // makes the intended visible dashboard-to-Drilldown gap 20px.
                    elementContainer.style.setProperty("margin-bottom", "4px", "important");
                    elementContainer.style.setProperty("padding-top", "0px", "important");
                    elementContainer.style.setProperty("padding-bottom", "0px", "important");
                }}
            }}
        }} catch (error) {{}}

        // The outer flow box has changed; realign the Drilldown after layout settles.
        try {{ window.parent.requestAnimationFrame(syncDrilldownGap); }} catch (error) {{}}
        try {{ window.parent.setTimeout(syncDrilldownGap, 40); }} catch (error) {{}}

        if (persist) {{
            saveDashboardHeight(height);
        }}
        publishLayout();
        return height;
    }}

    function setupDashboardBoundary() {{
        const stage = document.getElementById("npi-floating-stage");
        const boundary = document.getElementById("npi-dashboard-drilldown-boundary");
        if (!stage || !boundary || boundary.dataset.npiBoundaryWired === "1") return;
        boundary.dataset.npiBoundaryWired = "1";

        applyDashboardHeight(readDashboardHeight(), false);
        installDrilldownGapObserver();
        let boundaryDrag = null;

        boundary.addEventListener("pointerdown", function(event) {{
            if (event.button !== undefined && event.button !== 0) return;
            boundaryDrag = {{
                startY: event.clientY,
                startHeight: stage.clientHeight || DEFAULT_DASHBOARD_HEIGHT,
                pointerId: event.pointerId
            }};
            try {{ boundary.setPointerCapture(event.pointerId); }} catch (error) {{}}
            try {{ event.preventDefault(); }} catch (error) {{}}
        }});

        boundary.addEventListener("pointermove", function(event) {{
            if (!boundaryDrag) return;
            applyDashboardHeight(
                boundaryDrag.startHeight + event.clientY - boundaryDrag.startY,
                false
            );
            try {{ event.preventDefault(); }} catch (error) {{}}
        }});

        function finishBoundaryDrag() {{
            if (!boundaryDrag) return;
            boundaryDrag = null;
            applyDashboardHeight(stage.clientHeight || DEFAULT_DASHBOARD_HEIGHT, true);
        }}
        boundary.addEventListener("pointerup", finishBoundaryDrag);
        boundary.addEventListener("pointercancel", finishBoundaryDrag);
    }}

    /* ========================================================
       RESET BUTTON
       ======================================================== */

    function wireReset() {{

        const button =
            document.getElementById(
                "npi-reset-layout"
            );


        if (!button) {{
            return;
        }}


        button.addEventListener(
            "click",
            function(event) {{

                event.preventDefault();

                event.stopPropagation();

                resetLayout();

            }}
        );

    }}


    /* ========================================================
       INITIALISE
       ======================================================== */

    function initialise() {{

        const panels =
            document.querySelectorAll(
                ".npi-floating-panel"
            );


        panels.forEach(
            function(panel) {{

                enableDragging(
                    panel
                );


                enableResizeSaving(
                    panel
                );

            }}
        );


        loadLayout();
        setupDashboardBoundary();
        if (typeof ResizeObserver !== "undefined") {{
            let lastWidth=document.getElementById('npi-dashboard-scroll').clientWidth;
            const widthObserver=new ResizeObserver(function() {{
                const width=document.getElementById('npi-dashboard-scroll').clientWidth;
                if(Math.abs(width-lastWidth)>1 && !gestureDepth) {{lastWidth=width;reflowLayout();}}
            }});
            widthObserver.observe(document.getElementById('npi-dashboard-scroll'));
        }}

        wireLegend();

        wireDonutCenter();

        wireDelayBucketTiles();

        wireHotspotRows();

        wireDelayCommitmentTiles();

        wireReset();


        /*
         * The Streamlit parent DOM may finish rendering
         * the bridge buttons slightly after this component.
         *
         * Retry several times.
         */

        [
            0,
            100,
            250,
            500,
            800,
            1200,
            1800
        ].forEach(
            function(delay) {{

                setTimeout(
                    function() {{

                        hideBridgeButtons();

                    }},
                    delay
                );

            }}
        );

    }}


    initialise();

}})();

</script>
"""

    # ============================================================
    # 8. RENDER
    # ============================================================

    floating_html += "<script>" + _batch_a_touch_script("dashboard", "#npi-dashboard-scroll") + "</script>"
    components.html(
        floating_html,
        height=624,
        scrolling=False,
    )

    # The floating dashboard is rendered in an HTML iframe. Native Streamlit
    # widgets cannot be reliably driven from that iframe by synthetic DOM events,
    # so the live search form is rendered as native Streamlit controls immediately
    # below the floating dashboard. Its submit callback runs before the next app
    # rerun, allowing the Drilldown to update using the same unique-part dataset.
    with st.container(key="npi_enquiry_search_section", border=True):
        st.markdown("### ENQUIRY FINDER")
        st.caption("One full part number per line.")
        with st.form("npi_enquiry_search_form", clear_on_submit=False):
            st.text_area(
                "Full part number(s)",
                key="enquiry_input_text",
                height=60,
                placeholder="PART-001\nPART-002\nPART-003",
                help="Enter one full part number per line. The box scrolls for long lists; blank lines and duplicate entries are ignored.",
                label_visibility="collapsed",
            )
            st.form_submit_button(
                "SEARCH",
                type="primary",
                use_container_width=True,
                on_click=_run_enquiry_search,
            )
        search_message = st.session_state.get("enquiry_search_message", "")
        if search_message:
            st.caption(search_message)

    # Position the existing native form over its shared dashboard slot; no DOM moves.
    components.html(
        f"""<script>
(function(){{
    const parentWin=window.parent, parentDoc=parentWin.document, owner={json.dumps(dashboard_layout_owner)};
    const KEY='__npiEnquiryLayoutV5';
    const previous=parentWin[KEY];if(previous && previous.dispose)previous.dispose();
    // Retire old live controllers as well as ignoring their localStorage records.
    ['__npiEnquiryFinderFloatControllerV3','__npiEnquiryFinderFloatController'].forEach(function(key){{
        const old=parentWin[key];if(!old)return;
        try{{if(old.observer)old.observer.disconnect();if(old.resizeObserver)old.resizeObserver.disconnect();}}catch(e){{}}
        ['observerTimer','saveTimer'].forEach(name=>{{try{{if(old[name])parentWin.clearTimeout(old[name]);}}catch(e){{}}}});
        if(old.handlers)Object.entries(old.handlers).forEach(function(entry){{
            try{{(entry[0]==='resize'?parentWin:old.listenerDocument).removeEventListener(entry[0],entry[1],entry[0]==='resize'?false:true);}}catch(e){{}}
        }});
        old.retired=true;old.container=null;parentWin[key]=null;
    }});
    let panel=null,frame=null,api=null,drag=null,resizing=null,observer=null,raf=null,disposed=false;
    const removals=[];
    function listen(target,type,fn,options){{if(!target || !target.addEventListener)return;target.addEventListener(type,fn,options);removals.push(()=>target.removeEventListener(type,fn,options));}}
    function schedule(){{if(disposed || raf)return;raf=parentWin.requestAnimationFrame(function(){{
        raf=null;
        try{{sync();}}catch(e){{
            // Preserve the real native form if frontend geometry changes unexpectedly.
            if(panel){{
                panel.style.cssText='';
                let node=panel;
                while(node && node!==parentDoc.body){{
                    if(node.getAttribute && node.getAttribute('data-testid')==='stElementContainer'){{node.style.cssText='';break;}}
                    node=node.parentElement;
                }}
            }}
        }}
    }});}}
    function sync(){{
        if(disposed || drag || resizing)return;
        panel=parentDoc.querySelector('div[class*="st-key-npi_enquiry_search_section"]');
        if(!panel)return;
        frame=Array.from(parentDoc.querySelectorAll('iframe')).find(function(item){{
            try{{return item.contentWindow.__npiDashboardLayoutV5 && item.contentWindow.__npiDashboardLayoutV5.owner===owner;}}catch(e){{return false;}}
        }});
        if(!frame){{panel.style.setProperty('visibility','hidden');return;}}
        const next=frame.contentWindow.__npiDashboardLayoutV5;
        if(api!==next){{
            api=next;
            listen(frame.contentWindow,'npi-dashboard-layout',schedule);
            listen(frame.contentDocument.getElementById('npi-dashboard-scroll'),'scroll',schedule,{{passive:true}});
        }}
        const slot=frame.contentDocument.querySelector('[data-layout-id="enquiry"]');
        if(!slot)return;
        let block=panel;
        while(block && block!==parentDoc.body){{
            if(block.getAttribute && block.getAttribute('data-testid')==='stElementContainer'){{
                Object.assign(block.style,{{display:'contents',height:'0px',minHeight:'0px',margin:'0px',padding:'0px',overflow:'visible'}});break;
            }}block=block.parentElement;
        }}
        panel.style.position='absolute';
        const offset=panel.offsetParent || parentDoc.body;
        const base=offset.getBoundingClientRect(),fr=frame.getBoundingClientRect(),r=slot.getBoundingClientRect();
        const view=frame.contentDocument.getElementById('npi-dashboard-scroll').getBoundingClientRect();
        const top=fr.top+r.top,left=fr.left+r.left;
        const clipTop=Math.max(0,view.top-r.top),clipBottom=Math.max(0,r.bottom-view.bottom);
        const clipLeft=Math.max(0,view.left-r.left),clipRight=Math.max(0,r.right-view.right);
        const visible=clipTop+clipBottom<r.height && clipLeft+clipRight<r.width;
        const desktop=api.getLayout().mode==='desktop' && !!(parentWin.matchMedia && parentWin.matchMedia('(any-pointer: fine)').matches);
        Object.assign(panel.style,{{
            left:(left-base.left+offset.scrollLeft-offset.clientLeft)+'px',
            top:(top-base.top+offset.scrollTop-offset.clientTop)+'px',width:r.width+'px',height:r.height+'px',
            minWidth:'0px',minHeight:'0px',maxWidth:'none',maxHeight:'none',boxSizing:'border-box',
            padding:'10px',background:'#fff',border:'1px solid #d8e2ed',borderRadius:'12px',
            boxShadow:'0 3px 10px rgba(25,48,76,.07)',overflow:'auto',resize:desktop?'both':'none',
            zIndex:'100',visibility:visible?'visible':'hidden',
            clipPath:'inset('+clipTop+'px '+clipRight+'px '+clipBottom+'px '+clipLeft+'px)'
        }});
        const heading=panel.querySelector('h3');
        if(heading)Object.assign(heading.style,{{fontFamily:'Arial,Helvetica,sans-serif',fontSize:'12px',lineHeight:'18px',fontWeight:'800',
            margin:'-10px -10px 8px',padding:'10px 12px',background:'#20354f',color:'#fff',borderBottom:'1px solid #d8e2ed',
            cursor:desktop?'grab':'default',touchAction:desktop?'none':'auto',userSelect:'none'}});
        panel.querySelectorAll('[data-testid="stVerticalBlock"]').forEach(block=>block.style.gap='.3rem');
    }}
    listen(parentDoc,'pointerdown',function(event){{
        if(!panel || !api || api.getLayout().mode!=='desktop' || event.pointerType!=='mouse' || event.button!==0 || !panel.contains(event.target))return;
        const r=panel.getBoundingClientRect();
        if(event.clientX>=r.right-18 && event.clientY>=r.bottom-18){{resizing=true;api.beginGesture();return;}}
        if(!event.target.closest('h3'))return;
        const original=api.getLayout().rects.enquiry;
        drag={{x:event.clientX,y:event.clientY,left:original.left,top:original.top}};api.beginGesture();
        try{{event.target.closest('h3').setPointerCapture(event.pointerId);}}catch(e){{}}event.preventDefault();
    }},true);
    listen(parentDoc,'pointermove',function(event){{
        if(!drag)return;
        api.updatePanel('enquiry',{{left:Math.max(0,drag.left+event.clientX-drag.x),top:Math.max(0,drag.top+event.clientY-drag.y)}},false);
        // Temporarily release the sync guard to follow the shared slot.
        const keep=drag;drag=null;sync();drag=keep;
    }},true);
    function finish(){{
        if(!api || (!drag && !resizing))return;
        const patch=resizing?{{width:panel.offsetWidth,height:panel.offsetHeight}}:{{}};
        drag=null;resizing=null;api.endGesture();api.updatePanel('enquiry',patch,true);schedule();
    }}
    listen(parentDoc,'pointerup',finish,true);listen(parentDoc,'pointercancel',finish,true);
    listen(parentWin,'resize',schedule);listen(parentWin,'orientationchange',schedule);listen(parentWin.visualViewport,'resize',schedule);listen(parentWin,'scroll',schedule,{{capture:true,passive:true}});
    listen(parentDoc,'wheel',function(event){{
        if(!panel || !frame || !panel.contains(event.target))return;
        const target=event.target.closest('textarea');
        function canScroll(element){{return element && ((event.deltaY>0 && element.scrollTop+element.clientHeight<element.scrollHeight-1) || (event.deltaY<0 && element.scrollTop>0));}}
        if(canScroll(target) || canScroll(panel))return;
        const scroll=frame.contentDocument.getElementById('npi-dashboard-scroll');
        if(scroll.scrollHeight>scroll.clientHeight){{scroll.scrollTop+=event.deltaY;event.preventDefault();schedule();}}
    }},{{passive:false}});
    observer=new parentWin.MutationObserver(schedule);observer.observe(parentDoc.body,{{childList:true,subtree:true}});
    const timers=[0,100,300,700,1200].map(delay=>parentWin.setTimeout(schedule,delay));
    parentWin[KEY]={{owner,dispose:function(){{disposed=true;removals.forEach(fn=>fn());observer.disconnect();timers.forEach(t=>parentWin.clearTimeout(t));if(raf)parentWin.cancelAnimationFrame(raf);}}}};
    // The installer itself must not reserve space in the page flow.
    try{{
        const installer=window.frameElement;installer.style.cssText='position:absolute;width:1px;height:1px;opacity:0;pointer-events:none';
        let block=installer;while(block && block!==parentDoc.body){{
            if(block.getAttribute && block.getAttribute('data-testid')==='stElementContainer'){{
                block.style.cssText='position:absolute;width:1px;height:1px;min-height:0;margin:0;padding:0;overflow:hidden';break;
            }}block=block.parentElement;
        }}
    }}catch(e){{}}
    schedule();
}})();

        </script><script>
        (function() {{
            const script=window.parent.document.createElement('script');
            script.textContent={json.dumps(_batch_a_touch_script("enquiry", 'div[class*="st-key-npi_enquiry_search_section"]'))};
            window.parent.document.body.appendChild(script);script.remove();
        }})();
        </script>""",
        height=1,
        scrolling=False,
    )


def build_delay_bucket_summary(df):

    delay_bucket_order = [
        "<30 DAYS",
        "30–90 DAYS",
        "3–6 MONTHS",
        "6–12 MONTHS",
        "1–2 YEARS",
        ">2 YEARS",
    ]

    if (
        df is None
        or df.empty
        or "CALC_PRODUCTION_DELAY_BUCKET"
        not in df.columns
    ):
        return pd.DataFrame(
            {
                "Part Delay Days Bucket":
                    delay_bucket_order,

                "UNIQUE PARTS":
                    [0] * len(delay_bucket_order),
            }
        )

    # Delay Days Bucket is strictly for RED - PRODUCTION parts.
    # ON TRACK / other classifications must never contribute to these totals.
    source = df.copy()
    if "CALC_CLASSIFICATION" in source.columns:
        source = source[
            source["CALC_CLASSIFICATION"]
            .astype(str).str.strip().str.upper()
            == "RED - PRODUCTION"
        ].copy()

    values = source["CALC_PRODUCTION_DELAY_BUCKET"].astype(str).str.strip()

    # Normalize case and dash variants so labels such as "30-90 days"
    # match the display labels "30–90 DAYS" reliably.
    def normalize_bucket(value):
        value = str(value).strip().lower()
        for dash in ("–", "—", "−"):
            value = value.replace(dash, "-")
        value = " ".join(value.split())
        return value

    normalized_values = values.map(normalize_bucket)
    result = []

    for bucket in delay_bucket_order:
        normalized_bucket = normalize_bucket(bucket)
        result.append(
            {
                "Part Delay Days Bucket": bucket,
                "UNIQUE PARTS": int((normalized_values == normalized_bucket).sum()),
            }
        )

    return pd.DataFrame(result)


# ============================================================
# DRILLDOWN COLUMN MAPPING
# ============================================================

def get_engineer_column(df):

    return first_existing_column(
        df,
        [
            "Assigned Eng",
            "Assigned Engineer",
            "Engineer",
            "ENGINEER",
            "Engineer Name",
            "Engineering",
            "Engineering Owner",
        ],
    )


def get_job_column(df):

    return first_existing_column(
        df,
        [
            "JobNum/Asm",
            "Job Num/Asm",
            "JobNum",
            "JOB NO",
            "Job No",
            "Job Number",
            "Job",
        ],
    )


def get_current_column(df):

    return first_existing_column(
        df,
        [
            "CALC_CURRENT_OPERATION",
            "Current Operation",
            "Current Process",
            "CURRENT PROCESS",
        ],
    )


def get_next_column(df):

    return first_existing_column(
        df,
        [
            "CALC_NEXT_OPERATION",
            "Next Operation",
            "Next Process",
            "NEXT PROCESS",
        ],
    )


def get_delay_column(df):

    return first_existing_column(
        df,
        [
            "CALC_DELAY_DAYS",
            "Delay Days",
            "DELAY DAYS",
        ],
    )


def get_order_column(df):

    return first_existing_column(
        df,
        [
            "Order Date",
            "ORDER DATE",
            "M Order Date",
        ],
    )


def get_priority_column(df):

    return first_existing_column(
        df,
        [
            "CALC_PRIORITY",
            "Priority",
            "PRIORITY",
        ],
    )


def get_aging_days_column(df):

    return first_existing_column(
        df,
        [
            "CALC_ORDER_AGE_DAYS",
            "CALC_STAGE_AGING_DAYS",
            "CALC_PRODUCTION_DELAY_AGE",
            "CALC_AGING_DAYS",
            "AGING DAYS",
            "AGING",
            "Aging",
        ],
    )


# ============================================================
# DRILLDOWN
# ============================================================

def render_drilldown(df, title):
    """
    Control Tower drilldown.

    LOCKED HEADER STRUCTURE:

    CUS
    PART NO
    PICTURE
    QTY
    PO NUMBER
    ENGINEER
    JOB NO
    CURRENT PROCESS
    NEXT PROCESS
    DELAY DAYS
    ORDER DATE
    AGING
    PRIORITY
    COMMITMENT
        OWNER
        COMMITMENT DATE
        COMPLETION DATE
        REMARK

    Layout:
    - First 3 columns are horizontally frozen.
    - The first 3 columns remain frozen; QTY follows PICTURE in the scrollable area.
    - Two-level header is preserved.
    - PART NO width follows actual part number.
    - Other columns are controlled by heading/subheading width.
    - BOTH header rows are frozen during vertical scrolling.
    - Body row height = approximately 50 Excel points (~67 px).
    - Maximum 200 records per page.
    """

    # ========================================================
    # DRILLDOWN ANCHOR
    # ========================================================

    st.markdown(
        """
        <div
            id="npi-drilldown-anchor"
            style="
                position:relative;
                height:1px;
                margin:0;
                padding:0;
                scroll-margin-top:35px;
            "
        ></div>
        """,
        unsafe_allow_html=True,
    )

    customer_display = (
        st.session_state.get(
            "selected_customer",
            "ALL",
        )
    )

    if df is None or df.empty:

        st.info(
            "No records available for this view."
        )

        return

    # ========================================================
    # COLUMN RESOLUTION
    # ========================================================

    part_col = first_existing_column(
        df,
        [
            "PART NUMBER",
            "Part Number",
            "Subpart Part Num",
            "PART",
            "Part",
        ],
    )

    qty_col = first_existing_column(
        df,
        [
            "Subpart Qty",
            "SUBPART QTY",
            "Subpart Quantity",
            "QTY",
            "Qty",
        ],
    )

    po_col = first_existing_column(
        df,
        [
            "PO - POLine",
            "PO NUMBER",
            "PO Number",
        ],
    )

    customer_col = first_existing_column(
        df,
        [
            "CALC_CUSTOMER",
            "Customer",
            "CUSTOMER",
            "Cust",
            "CUS",
        ],
    )

    engineer_col = get_engineer_column(
        df
    )

    job_col = get_job_column(
        df
    )

    current_col = get_current_column(
        df
    )

    next_col = get_next_column(
        df
    )

    delay_col = get_delay_column(
        df
    )

    order_col = get_order_column(
        df
    )

    priority_col = get_priority_column(
        df
    )

    aging_col = get_aging_days_column(
        df
    )

    # ========================================================
    # PART DETAIL POP-UP DATA HELPERS
    # ========================================================

    def detail_source_value(source_row, aliases, value_type="text"):
        if source_row is None or not hasattr(source_row, "index"):
            return ""
        source_col = first_existing_column(source_row.to_frame().T, aliases)
        if not source_col:
            return ""
        value = source_row.get(source_col, "")
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        if value_type == "date":
            return safe_text(fmt_date(value))
        if value_type == "number":
            return safe_text(fmt_number(value))
        return safe_text(value)

    # Part Detail reads directly from the selected row in Unique Part Dataset.
    # These are BAQ headers; keep the aliases broad for existing calculated columns.
    detail_field_aliases = [
        ("Part Number", ["Subpart Part Num", "PART NUMBER", "Part Number", "PART", "Part"], "text"),
        ("PO Number", ["PO - POLine", "PO NUMBER", "PO Number"], "text"),
        ("Engineer", ["Assigned Eng", "Assigned Engineer", "Engineer", "ENGINEER", "Engineer Name", "Engineering", "Engineering Owner"], "text"),
        ("Job Number", ["JobNum/Asm", "Job Num/Asm", "JobNum", "JOB NO", "Job No", "Job Number", "Job"], "text"),
        ("Nesting Number", ["Nesting Num", "Nesting Number", "Nesting No", "Nesting", "NestingNumber", "Nest Number"], "text"),
        ("Order Date", ["Order Date", "ORDER DATE", "M Order Date"], "date"),
        ("Exwork Date", ["Exwork Date", "Ex-Work Date", "Ex Work Date", "EXWORK DATE", "M Exwork Date", "Ex-Work", "Exwork"], "date"),
        ("First Process Plan Date", ["First Process Plan Date"], "date"),
        ("Delay Days", ["CALC_DELAY_DAYS", "TEST23C - Delay Days", "DELAY DAYS", "DELAY DAYS.1", "Delay Days"], "number"),
        ("Aging", ["CALC_ORDER_AGE_DAYS", "CALC_STAGE_AGING_DAYS", "CALC_PRODUCTION_DELAY_AGE", "CALC_AGING_DAYS", "AGING DAYS", "AGING", "Aging"], "number"),
        ("Material", ["Mtl 10", "Material", "MATERIAL", "Subpart Material", "Material Description"], "text"),
        ("Qty", ["Subpart Qty", "SUBPART QTY", "Subpart Quantity", "QTY", "Qty"], "number"),
        ("Finishing", ["Finishing", "FINISHING", "Subpart Finishing"], "text"),
        ("Plating", ["Plating", "PLATING", "Subpart Plating"], "text"),
        ("Subpart 2D Rev", ["Subpart 2D Rev", "Subpart 2D Revision", "2D Rev", "2D Revision"], "text"),
        ("Subpart 3D Rev", ["Subpart 3D Rev", "Subpart 3D Revision", "3D Rev", "3D Revision"], "text"),
        ("Subpart KK Rev", ["Subpart KK Rev", "Subpart KK Revision", "KK Rev", "KK Revision"], "text"),
    ]

    def build_part_detail_payload(source_row, part_key):
        payload = {
            label: detail_source_value(source_row, aliases, value_type)
            for label, aliases, value_type in detail_field_aliases
        }
        picture = None
        if part_key:
            picture = picture_map.get(part_key)
            if picture is None:
                try:
                    picture = get_picture_for_part(picture_map, part_key)
                except Exception:
                    picture = None
        picture_data = ""
        if picture is not None:
            try:
                if isinstance(picture, (bytes, bytearray, memoryview)):
                    picture_bytes = bytes(picture)
                elif hasattr(picture, "save"):
                    picture_stream = BytesIO()
                    picture.save(picture_stream, format="PNG")
                    picture_bytes = picture_stream.getvalue()
                else:
                    picture_bytes = b""
                picture_data = image_to_base64(picture_bytes) or ""
            except Exception:
                picture_data = ""
        payload["Picture"] = ("data:image/png;base64," + picture_data) if picture_data else ""

        # The clicked row already comes from the filtered Unique Part Dataset.
        # Read Step 1..20 and each matching Job Oper date pair from this exact row;
        # never re-query raw/calc data by Part Number, which could select another record.
        payload["Process Routing"] = []
        excluded_operations = {"COMPLETE", "COMPLETED", "N/A", "NA", "NONE", "-", "NAN"}
        for step_no in range(1, 21):
            operation = detail_source_value(source_row, [f"Step {step_no}"])
            if not operation or operation.strip().upper() in excluded_operations:
                continue
            job_oper_no = step_no * 10
            payload["Process Routing"].append({
                "Operation": operation,
                "Plan Start Date": detail_source_value(
                    source_row,
                    [f"Job Oper {job_oper_no} Plan Start Date"],
                    "date",
                ),
                "Actual Start Date": detail_source_value(
                    source_row,
                    [f"Job Oper {job_oper_no} Actual Start Date"],
                    "date",
                ),
            })
        return payload

    # Prepare responsive header groups. The Current Process popover must be created
    # before sorting/pagination so its selection can filter the data on this run.
    st.markdown(
        """
        <style>
div.st-key-drilldown_compact_header { container-type:inline-size; container-name:npiDD; min-width:0; max-width:100%; }
div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) { display:grid !important; grid-template-columns:minmax(0,1.4fr) minmax(130px,.7fr) minmax(270px,1.3fr) minmax(150px,.7fr); gap:12px !important; align-items:center; }
div.st-key-drilldown_compact_header :is([data-testid="column"],[data-testid="stColumn"]) { min-width:0 !important; width:auto !important; max-width:100% !important; flex:none !important; }
div.st-key-drilldown_compact_header [data-testid="stVerticalBlock"] { gap:4px; }
div.st-key-drilldown_compact_header .npi-dd-title { font-size:18px; font-weight:700; color:#20354f; line-height:1.3; margin:0; overflow-wrap:anywhere; }
div.st-key-drilldown_compact_header .npi-dd-meta { font-size:12px; color:#64748b; line-height:1.4; margin:0; white-space:normal; overflow-wrap:anywhere; }
div.st-key-drilldown_compact_header :is(button,a) { min-height:36px; height:auto; white-space:normal !important; overflow-wrap:anywhere; max-width:100%; }
div.st-key-npi_dd_navigation [data-testid="stHorizontalBlock"] { display:grid !important; grid-template-columns:minmax(54px,1fr) 40px minmax(54px,1fr) 40px minmax(34px,.6fr); gap:4px !important; }
div.st-key-npi_dd_navigation button { width:100%; padding:4px !important; }
@container npiDD (max-width:1199px) {
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) { grid-template-columns:minmax(0,1fr) minmax(0,1fr); }
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) > :is([data-testid="column"],[data-testid="stColumn"]):has(.st-key-npi_dd_navigation) { grid-column:1; grid-row:2; }
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) > :is([data-testid="column"],[data-testid="stColumn"]):has(.st-key-npi_dd_export) { grid-column:2; grid-row:2; }
 div.st-key-drilldown_compact_header :is(button,a) { min-height:42px; }
}
@container npiDD (max-width:650px) {
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) { gap:8px !important; }
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) > :is([data-testid="column"],[data-testid="stColumn"]):has(.st-key-npi_dd_summary) { grid-column:1/-1; grid-row:1; }
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) > :is([data-testid="column"],[data-testid="stColumn"]):has(.st-key-npi_dd_filter) { grid-column:1; grid-row:2; }
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) > :is([data-testid="column"],[data-testid="stColumn"]):has(.st-key-npi_dd_export) { grid-column:2; grid-row:2; }
 div.st-key-drilldown_compact_header [data-testid="stHorizontalBlock"]:has(.st-key-npi_dd_summary) > :is([data-testid="column"],[data-testid="stColumn"]):has(.st-key-npi_dd_navigation) { grid-column:1/-1; grid-row:3; }
}
</style>
        """,
        unsafe_allow_html=True,
    )

    current_process_filter_key = "drilldown_current_process_filter"
    process_options = ["ALL"]
    process_option_labels = {"ALL": f"ALL{chr(160) * 7}{len(df):,}"}
    if current_col and current_col in df.columns:
        process_values = (
            df[current_col]
            .dropna()
            .astype(str)
            .str.strip()
        )
        process_values = process_values[
            process_values.ne("") & ~process_values.str.lower().isin({"nan", "none"})
        ]
        process_counts = process_values.value_counts().to_dict()
        ordered_processes = sorted(
            process_counts,
            key=lambda value: (-process_counts[value], value.casefold()),
        )
        process_options.extend(ordered_processes)
        process_option_labels.update(
            {value: f"{value}{chr(160) * 7}{process_counts[value]:,}" for value in ordered_processes}
        )

    if st.session_state.get(current_process_filter_key, "ALL") not in process_options:
        st.session_state[current_process_filter_key] = "ALL"

    selected_current_process = st.session_state.get(current_process_filter_key, "ALL")

    with st.container(key="drilldown_compact_header"):
        header_cols = st.columns([3, 1.5, 3, 1.5], vertical_alignment="center", gap="small")
        with header_cols[0], st.container(key="npi_dd_summary"):
            header_title_slot = st.empty()
            header_customer_slot = st.empty()
        with header_cols[1], st.container(key="npi_dd_filter"):
            header_process_slot = st.empty()
        with header_cols[2], st.container(key="npi_dd_navigation"):
            header_page_slot = st.empty()
            header_records_slot = st.empty()
        with header_cols[3], st.container(key="npi_dd_export"):
            header_export_slot = st.empty()

        with header_process_slot.container():
            with st.popover(
                "Current Process",
                key="drilldown_current_process_popover",
                use_container_width=True,
            ):
                st.caption(
                    "Active filter: "
                    + (selected_current_process if selected_current_process != "ALL" else "ALL processes")
                )

                def _reset_current_process_filter():
                    st.session_state[current_process_filter_key] = "ALL"
                    for state_key in list(st.session_state.keys()):
                        if str(state_key).startswith("drilldown_page_state_"):
                            st.session_state[state_key] = 1

                st.selectbox(
                    "Current Process",
                    process_options,
                    format_func=lambda value: process_option_labels.get(value, value),
                    key=current_process_filter_key,
                    label_visibility="collapsed",
                    on_change=lambda: [
                        st.session_state.__setitem__(state_key, 1)
                        for state_key in list(st.session_state.keys())
                        if str(state_key).startswith("drilldown_page_state_")
                    ],
                )
                st.button(
                    "Clear process filter",
                    key="drilldown_clear_current_process_filter",
                    use_container_width=True,
                    on_click=_reset_current_process_filter,
                )

    selected_current_process = st.session_state.get(current_process_filter_key, "ALL")
    if selected_current_process != "ALL" and current_col and current_col in df.columns:
        df = df[
            df[current_col].fillna("").astype(str).str.strip().eq(selected_current_process)
        ].copy()

    # ========================================================
    # SORT
    # ========================================================

    display_df = df.copy()

    # Keep the header count aligned with the extra Current Process filter.
    if selected_current_process != "ALL":
        title_prefix = title.rsplit(" (", 1)[0] if title.endswith(")") and " (" in title else title
        title = f"{title_prefix} ({len(display_df):,})"

    if (
        delay_col
        and delay_col in display_df.columns
    ):

        display_df["_SORT_DELAY"] = (
            pd.to_numeric(
                display_df[delay_col],
                errors="coerce",
            )
            .fillna(0)
        )

        display_df = display_df.sort_values(
            "_SORT_DELAY",
            ascending=False,
            kind="stable",
        )

    # ========================================================
    # PAGINATION
    # ========================================================

    page_size = 200

    total_rows = len(
        display_df
    )

    total_pages = max(
        1,
        (
            total_rows
            + page_size
            - 1
        )
        // page_size,
    )

    # ========================================================
    # RESPONSIVE DRILLDOWN HEADER + PAGINATION
    # ========================================================

    # Store the current page independently of Streamlit widget state so that
    # changing a legend tile / Back to Top does not leave stale page controls.
    page_state_key = f"drilldown_page_state_{title}"
    page = int(st.session_state.get(page_state_key, 1))
    page = max(1, min(page, total_pages))
    st.session_state[page_state_key] = page

    # Fill the header placeholders after the current-process filter and
    # pagination totals have been calculated. Export uses its responsive group.
    meta_style = "font-size:12px;color:#64748b;line-height:1.4;"
    header_title_slot.markdown(
        f"<div class='npi-dd-title' style='font-size:18px;font-weight:700;'>"
        f"{html_escape(title)}</div>",
        unsafe_allow_html=True,
    )
    header_customer_slot.markdown(
        f"<div class='npi-dd-meta' style='{meta_style}'>CUSTOMER: "
        f"{html_escape(str(customer_display))}</div>",
        unsafe_allow_html=True,
    )

    with header_page_slot.container():
        page_controls = st.columns(
            [1.2, 0.45, 0.85, 0.45, 0.65],
            vertical_alignment="center",
            gap="small",
        )
        page_controls[0].button(
            "First …",
            key=f"drilldown_first_page_{title}",
            disabled=(page <= 1),
            on_click=lambda k=page_state_key: st.session_state.__setitem__(k, 1),
        )
        page_controls[1].button(
            "−",
            key=f"drilldown_prev_page_{title}",
            disabled=(page <= 1),
            on_click=lambda k=page_state_key, p=page: st.session_state.__setitem__(k, max(1, p - 1)),
        )
        page_controls[2].markdown(
            f"<div class='npi-dd-meta' style='{meta_style}text-align:center;'>Page {page}</div>",
            unsafe_allow_html=True,
        )
        page_controls[3].button(
            "+",
            key=f"drilldown_next_page_{title}",
            disabled=(page >= total_pages),
            on_click=lambda k=page_state_key, p=page, n=total_pages: st.session_state.__setitem__(k, min(n, p + 1)),
        )
        page_controls[4].markdown(
            f"<div class='npi-dd-meta' style='{meta_style}'>of {total_pages}</div>",
            unsafe_allow_html=True,
        )

    header_records_slot.markdown(
        f"<div class='npi-dd-meta' style='{meta_style}'>Showing "
        f"{(page - 1) * page_size + 1:,}–"
        f"{min(page * page_size, total_rows):,} of {total_rows:,} records</div>",
        unsafe_allow_html=True,
    )


    start_idx = (page - 1) * page_size
    end_idx = min(start_idx + page_size, total_rows)

    page_df = display_df.iloc[
        start_idx:end_idx
    ].copy()

    # ========================================================
    # SHARED SUPABASE COMMITMENTS
    # ========================================================

    saved_message = st.session_state.pop("_npi_commitment_success_message", None)
    if saved_message:
        st.success(saved_message)

    commitment_lookup = {}
    current_user_id = safe_text(st.session_state.get("npi_user_id")).strip()
    if st.session_state.get("_npi_popup_owner") != current_user_id:
        st.session_state["_npi_popup_owner"] = current_user_id
        st.session_state["_npi_popup_bridge_id"] = py_secrets.token_urlsafe(24)
        st.session_state["_npi_popup_requests"] = {}
    popup_bridge_id = st.session_state["_npi_popup_bridge_id"]
    popup_requests = st.session_state["_npi_popup_requests"]
    if current_user_id:
        part_keys_for_commitments = []
        if part_col and part_col in display_df.columns:
            part_keys_for_commitments = [
                safe_text(value).strip()
                for value in display_df[part_col].tolist()
                if safe_text(value).strip()
            ]
        try:
            commitment_lookup = get_supabase_commitments(
                part_keys_for_commitments, user_id=current_user_id
            )
        except Exception as commitment_error:
            st.warning(
                "Unable to load shared Supabase commitment records. "
                f"The dashboard is still available, but commitment fields may be blank. ({commitment_error})"
            )
            commitment_lookup = {}

    # Save one batch event from the drilldown or a single Part Detail popup.
    # ========================================================
    # ACTION LOOKUP (legacy BAQ/action data remains fallback only)
    # ========================================================

    actions_lookup = {}

    if (
        "actions_df" in st.session_state
        and st.session_state.actions_df
        is not None
        and not st.session_state.actions_df.empty
    ):

        actions_df = (
            st.session_state.actions_df.copy()
        )

        if "PART_NUMBER" in actions_df.columns:

            for _, action_row in (
                actions_df.iterrows()
            ):

                part_key = safe_text(
                    action_row.get(
                        "PART_NUMBER",
                        "",
                    )
                ).strip()

                if part_key:

                    actions_lookup[
                        part_key
                    ] = action_row

    # ========================================================
    # HELPER FUNCTIONS
    # ========================================================

    def get_value(
        row,
        col,
    ):

        if not col:
            return ""

        if col not in row.index:
            return ""

        value = row.get(
            col
        )

        try:

            if pd.isna(value):
                return ""

        except Exception:
            pass

        return value


    def display_value(
        row,
        col,
        value_type="text",
    ):

        value = get_value(
            row,
            col,
        )

        if value_type == "date":
            return fmt_date(value)

        if value_type == "number":
            return fmt_number(value)

        return safe_text(value)


    def get_action_value(
        action,
        aliases,
    ):

        if not isinstance(
            action,
            dict,
        ):
            return ""

        for field in aliases:

            if field in action:

                value = action.get(
                    field
                )

                try:

                    if pd.isna(value):
                        continue

                except Exception:
                    pass

                if safe_text(
                    value
                ).strip():

                    return value

        return ""

    # ========================================================
    # COLUMN DEFINITIONS
    # ========================================================

    fixed_columns = [
        {
            "key": "picture",
            "heading": "PICTURE",
            "source": None,
            "type": "picture",
        },
        {
            "key": "customer",
            "heading": "CUS",
            "source": customer_col,
            "type": "text",
        },
        {
            "key": "part",
            "heading": "PART NO",
            "source": part_col,
            "type": "part",
        },
        {
            "key": "qty",
            "heading": "QTY",
            "source": qty_col,
            "type": "number",
        },
        {
            "key": "po_number",
            "heading": "PO NUMBER",
            "source": po_col,
            "type": "text",
        },
        {
            "key": "engineer",
            "heading": "ENGINEER",
            "source": engineer_col,
            "type": "text",
        },
        {
            "key": "job",
            "heading": "JOB NO",
            "source": job_col,
            "type": "text",
        },
        {
            "key": "current",
            "heading": "CURRENT PROCESS",
            "source": current_col,
            "type": "text",
        },
        {
            "key": "next",
            "heading": "NEXT PROCESS",
            "source": next_col,
            "type": "text",
        },
        {
            "key": "delay",
            "heading": "DELAY DAYS",
            "source": delay_col,
            "type": "number",
        },
        {
            "key": "order_date",
            "heading": "ORDER DATE",
            "source": order_col,
            "type": "date",
        },
        {
            "key": "aging",
            "heading": "AGING",
            "source": aging_col,
            "type": "number",
        },
        {
            "key": "priority",
            "heading": "PRIORITY",
            "source": priority_col,
            "type": "text",
        },
    ]

    commitment_columns = [
        {
            "key": "owner",
            "heading": "OWNER",
        },
        {
            "key": "commitment_date",
            "heading": "COMMITMENT DATE",
        },
        {
            "key": "actual_completion",
            "heading": "COMPLETION DATE",
        },
        {
            "key": "remarks",
            "heading": "REMARK",
        },
    ]

    # ========================================================
    # PART NUMBER WIDTH
    # ========================================================

    part_values = []

    if (
        part_col
        and part_col in display_df.columns
    ):

        for value in (
            display_df[part_col].tolist()
        ):

            try:

                if pd.isna(value):
                    continue

            except Exception:
                pass

            text_value = (
                safe_text(value)
                .strip()
            )

            if text_value:

                part_values.append(
                    text_value
                )

    longest_part = max(
        part_values,
        key=len,
        default="PART NO",
    )

    part_width_px = max(
        130,
        min(
            360,
            20
            + (
                len(longest_part)
                * 8
            ),
        ),
    )

    # ========================================================
    # COLUMN WIDTHS
    # ========================================================

    heading_widths = {
        "CUS": 65,
        "PART NO": part_width_px,
        "PICTURE": 72,
        "QTY": 70,
        "PO NUMBER": 115,
        "ENGINEER": 105,
        "JOB NO": 100,
        "CURRENT PROCESS": 135,
        "NEXT PROCESS": 125,
        "DELAY DAYS": 95,
        "ORDER DATE": 105,
        "AGING": 80,
        "PRIORITY": 80,

        "OWNER": 100,
        "COMMITMENT DATE": 125,
        "COMPLETION DATE": 130,
        "REMARK": 180,
    }

    # ========================================================
    # STICKY COLUMN POSITIONS
    # ========================================================

    cus_width = heading_widths[
        "CUS"
    ]

    part_width = heading_widths[
        "PART NO"
    ]

    sticky_left = {"PICTURE": 0}

    # ========================================================
    # EXCEL EXPORT — MATCH THE DRILLDOWN TABLE
    # ========================================================

    try:
        from io import BytesIO
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XLImage
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Drilldown"

        header_fill = PatternFill("solid", fgColor="F2F2F2")
        header_font = Font(name="Arial", size=10, bold=True)
        body_font = Font(name="Arial", size=10)
        thin_grey = Side(style="thin", color="BDBDBD")
        cell_border = Border(left=thin_grey, right=thin_grey, top=thin_grey, bottom=thin_grey)
        center_wrap = Alignment(horizontal="center", vertical="center", wrap_text=True)
        body_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # Two-level header: fixed columns span both header rows; COMMITMENT spans the requested subcolumns.
        for col_index, col in enumerate(fixed_columns, start=1):
            cell = worksheet.cell(row=1, column=col_index, value=col["heading"])
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center_wrap
            cell.border = cell_border
            worksheet.merge_cells(start_row=1, start_column=col_index, end_row=2, end_column=col_index)
            # Apply style to both cells in the merged header range.
            worksheet.cell(row=2, column=col_index).fill = header_fill
            worksheet.cell(row=2, column=col_index).border = cell_border

        commitment_start = len(fixed_columns) + 1
        commitment_end = commitment_start + len(commitment_columns) - 1
        worksheet.merge_cells(start_row=1, start_column=commitment_start, end_row=1, end_column=commitment_end)
        commitment_header = worksheet.cell(row=1, column=commitment_start, value="COMMITMENT")
        commitment_header.fill = header_fill
        commitment_header.font = header_font
        commitment_header.alignment = center_wrap
        commitment_header.border = cell_border

        for offset, col in enumerate(commitment_columns):
            cell = worksheet.cell(row=2, column=commitment_start + offset, value=col["heading"])
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = center_wrap
            cell.border = cell_border

        # Match the table's column order and displayed values. Export every sorted row, not just page_df.
        excel_row = 3
        for _, row in display_df.iterrows():
            part_key = safe_text(get_value(row, part_col)).strip()
            action = actions_lookup.get(part_key, {})
            excel_col = 1
            for col in fixed_columns:
                cell = worksheet.cell(row=excel_row, column=excel_col)
                cell.font = body_font
                cell.alignment = body_alignment
                cell.border = cell_border

                if col["type"] == "picture":
                    picture = picture_map.get(part_key)
                    if picture is None:
                        try:
                            picture = get_picture_for_part(picture_map, part_key)
                        except Exception:
                            picture = None
                    image_stream = None
                    try:
                        if isinstance(picture, (bytes, bytearray, memoryview)):
                            image_stream = BytesIO(bytes(picture))
                        elif picture is not None and hasattr(picture, "save"):
                            image_stream = BytesIO()
                            picture.save(image_stream, format="PNG")
                            image_stream.seek(0)
                        if image_stream is not None:
                            excel_image = XLImage(image_stream)
                            original_width = max(1, excel_image.width)
                            original_height = max(1, excel_image.height)
                            scale = min(80 / original_width, 60 / original_height, 1)
                            excel_image.width = max(1, int(original_width * scale))
                            excel_image.height = max(1, int(original_height * scale))
                            worksheet.add_image(excel_image, cell.coordinate)
                    except Exception:
                        # Keep the picture cell blank if an individual source image cannot be embedded.
                        pass
                elif col["type"] == "part":
                    cell.value = part_key
                elif col["type"] in ("date", "number"):
                    cell.value = display_value(row, col["source"], col["type"])
                else:
                    cell.value = display_value(row, col["source"], "text")
                excel_col += 1

            for col in commitment_columns:
                key = col["key"]
                aliases_by_key = {
                    "owner": ["OWNER", "Owner"],
                    "action": ["ACTION", "Action"],
                    "support_required": ["SUPPORT_REQUIRED", "Support Required", "SUPPORT", "Support"],
                    "commitment_date": ["COMMITMENT_DATE", "Commitment Date"],
                    "action_status": ["ACTION_STATUS", "Action Status", "STATUS", "Commitment Status"],
                    "actual_completion": ["ACTUAL_COMPLETION", "Actual Completion", "COMPLETION_DATE", "Completion Date"],
                    "remarks": ["REMARKS", "Remarks", "NOTE", "Notes"],
                }
                value = get_action_value(action, aliases_by_key[key])
                if key in ("commitment_date", "actual_completion") and value:
                    value = fmt_date(value)
                cell = worksheet.cell(row=excel_row, column=excel_col, value=safe_text(value))
                cell.font = body_font
                cell.alignment = body_alignment
                cell.border = cell_border
                excel_col += 1

            worksheet.row_dimensions[excel_row].height = 50
            excel_row += 1

        worksheet.row_dimensions[1].height = 31.5
        worksheet.row_dimensions[2].height = 31.5
        # Approximate the on-screen pixel widths in Excel column-width units.
        for col_index, col in enumerate(fixed_columns, start=1):
            width_px = heading_widths.get(col["heading"], 100)
            worksheet.column_dimensions[worksheet.cell(row=1, column=col_index).column_letter].width = max(9, round(width_px / 7))
        for offset, col in enumerate(commitment_columns):
            width_px = heading_widths.get(col["heading"], 100)
            letter = worksheet.cell(row=2, column=commitment_start + offset).column_letter
            worksheet.column_dimensions[letter].width = max(12, round(width_px / 7))

        worksheet.freeze_panes = "A3"
        excel_buffer = BytesIO()
        workbook.save(excel_buffer)
        header_export_slot.download_button(
            "Download Drilldown Excel",
            data=excel_buffer.getvalue(),
            file_name="NPI_Control_Tower_Drilldown.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_current_drilldown_excel",
            use_container_width=True,
        )
    except Exception as exc:
        header_export_slot.warning(f"Excel export is unavailable: {exc}")

    # ========================================================
    # HEADER / ROW HEIGHTS
    # ========================================================

    MAIN_HEADER_HEIGHT = 42
    SUB_HEADER_HEIGHT = 42

    TOTAL_HEADER_HEIGHT = (
        MAIN_HEADER_HEIGHT
        + SUB_HEADER_HEIGHT
    )

    BODY_ROW_HEIGHT = 67

    # ========================================================
    # HTML TABLE
    # ========================================================

    html_parts = []

    html_parts.append(
        """
        <style>
            html,body { margin:0; padding:0; width:100%; max-width:100%; overflow-x:hidden; }
            .npi-drilldown-wrap {
                min-width:0;
                max-width:100%;
                box-sizing:border-box;
                border: 1px solid #d8e0ea;
                border-radius: 10px;
                overflow: auto;
                overflow-x: auto !important;
                overflow-y: auto !important;
                -webkit-overflow-scrolling: touch;
                touch-action: pan-x pan-y;
                overscroll-behavior-x: contain;
                background: #ffffff;
                box-shadow: 0 2px 8px rgba(15, 35, 60, 0.07);
                scrollbar-color: #aebdce #f3f6fa;
                scrollbar-width: thin;
            }
            .npi-drilldown-table {
                border-collapse: separate;
                border-spacing: 0;
                table-layout: fixed;
                width: max-content;
                min-width: 100%;
                font-family: Inter, Segoe UI, Arial, sans-serif;
                font-size: 12px;
                color: #233247;
            }
            .npi-drilldown-table tbody tr:hover td {
                background: #eaf3ff !important;
            }
            .npi-drilldown-table tbody tr.npi-part-detail-clickable {
                cursor: pointer;
            }
            .npi-drilldown-table tbody td {
                transition: background-color 100ms ease-in-out;
            }
        </style>
        """
    )

    # Keep the save toolbar outside the horizontal-scroll container so it stays
    # visible while the user scrolls across to the commitment columns.
    html_parts.append(
        """
        <div class="npi-commitment-toolbar" style="position:sticky;top:0;z-index:1000;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:9px 10px;background:#f7fafc;border:1px solid #d8e0ea;border-bottom:0;border-radius:10px 10px 0 0;">
            <span id="npi-commitment-save-status" role="status" style="flex:1;min-width:0;font:600 11px Inter,Segoe UI,Arial,sans-serif;color:#526477;">Edit any number of parts, then save all changes once.</span>
            <button type="button" id="npi-save-commitment" disabled style="flex:0 0 auto;margin-left:auto;border:0;border-radius:7px;background:#087f8c;color:#fff;padding:8px 14px;font:700 12px Inter,Segoe UI,Arial,sans-serif;cursor:pointer;opacity:.55;">Save Commitment</button>
        </div>
        """
    )

    html_parts.append(
        f"""
        <div class="npi-drilldown-wrap" style="
            width:100%;
            max-height:calc(100vh - 220px);
            min-height:553px;
            position:relative;
            border-top-left-radius:0;
            border-top-right-radius:0;
        ">
        """
    )

    html_parts.append(
        """
        <table class="npi-drilldown-table">
        """
    )

    # ========================================================
    # COLGROUP
    # ========================================================

    html_parts.append(
        "<colgroup>"
    )

    for col in fixed_columns:

        width = heading_widths.get(
            col["heading"],
            100,
        )

        html_parts.append(
            f"""
            <col style="
                width:{width}px;
                min-width:{width}px;
                max-width:{width}px;
            ">
            """
        )

    for col in commitment_columns:

        width = heading_widths.get(
            col["heading"],
            100,
        )

        html_parts.append(
            f"""
            <col style="
                width:{width}px;
                min-width:{width}px;
                max-width:{width}px;
            ">
            """
        )

    html_parts.append(
        "</colgroup>"
    )

    # ========================================================
    # HEADER — TWO ROWS
    # ========================================================

    html_parts.append(
        """
        <thead>
        """
    )

    html_parts.append(
        f"""
        <tr style="
            height:{MAIN_HEADER_HEIGHT}px;
        ">
        """
    )

    for col in fixed_columns:

        heading = col[
            "heading"
        ]

        width = heading_widths.get(
            heading,
            100,
        )

        sticky_style = ""

        if heading in sticky_left:

            sticky_style = f"""
                left:{sticky_left[heading]}px;
                z-index:50;
            """

        else:

            sticky_style = """
                z-index:40;
            """

        html_parts.append(
            f"""
            <th class="{'npi-frozen-col' if heading in sticky_left else ''}"
                rowspan="2"
                style="
                    width:{width}px;
                    min-width:{width}px;
                    max-width:{width}px;
                    height:{TOTAL_HEADER_HEIGHT}px;
                    padding:5px 5px;
                    border-right:1px solid #d3deea;
                    border-bottom:1px solid #d3deea;
                    border-left:1px solid #d3deea;
                    text-align:center;
                    vertical-align:middle;
                    font-weight:700;
                    line-height:1.15;
                    white-space:normal;
                    overflow-wrap:break-word;
                    box-sizing:border-box;

                    position:sticky !important;
                    top:0px !important;

                    background:#20354f !important;
                    color:#ffffff !important;

                    {sticky_style}
                "
            >
                {html_escape(heading)}
            </th>
            """
        )

    commitment_total_width = sum(
        heading_widths.get(
            col["heading"],
            100,
        )
        for col in commitment_columns
    )

    html_parts.append(
        f"""
        <th
            colspan="{len(commitment_columns)}"
            style="
                width:{commitment_total_width}px;
                min-width:{commitment_total_width}px;
                height:{MAIN_HEADER_HEIGHT}px;
                padding:5px 5px;
                border-right:1px solid #d3deea;
                border-bottom:1px solid #d3deea;
                border-left:1px solid #d3deea;
                text-align:center;
                vertical-align:middle;
                font-weight:700;
                line-height:1.15;
                white-space:normal;
                box-sizing:border-box;

                position:sticky !important;
                top:0px !important;

                z-index:45;
                background:#167d9a !important;
                color:#ffffff !important;
            "
        >
            COMMITMENT
        </th>
        """
    )

    html_parts.append(
        "</tr>"
    )

    html_parts.append(
        f"""
        <tr style="
            height:{SUB_HEADER_HEIGHT}px;
        ">
        """
    )

    for col in commitment_columns:

        heading = col[
            "heading"
        ]

        width = heading_widths.get(
            heading,
            100,
        )

        html_parts.append(
            f"""
            <th
                style="
                    width:{width}px;
                    min-width:{width}px;
                    max-width:{width}px;
                    height:{SUB_HEADER_HEIGHT}px;
                    padding:5px 5px;
                    border-right:1px solid #d3deea;
                    border-bottom:1px solid #d3deea;
                    border-left:1px solid #d3deea;
                    text-align:center;
                    vertical-align:middle;
                    font-weight:700;
                    line-height:1.15;
                    white-space:normal;
                    overflow-wrap:break-word;
                    box-sizing:border-box;

                    position:sticky !important;
                    top:{MAIN_HEADER_HEIGHT}px !important;

                    z-index:44;
                    background:#e8f1f8 !important;
                    color:#20354f !important;
                "
            >
                {html_escape(heading)}
            </th>
            """
        )

    html_parts.append(
        "</tr>"
    )

    html_parts.append(
        "</thead>"
    )

    # ========================================================
    # BODY
    # ========================================================

    html_parts.append(
        "<tbody>"
    )

    def commitment_date_input_value(value):
        if value is None or value == "":
            return ""
        try:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return ""
            return parsed.strftime("%Y-%m-%d")
        except Exception:
            return ""

    for row_index, (_, row) in enumerate(page_df.iterrows()):

        row_background = "#ffffff" if row_index % 2 == 0 else "#f6f8fb"

        part_value = get_value(
            row,
            part_col,
        )

        part_key = safe_text(
            part_value
        ).strip()

        action = actions_lookup.get(
            part_key,
            {},
        )

        part_detail_payload = build_part_detail_payload(row, part_key)
        stored_commitment = commitment_lookup.get(part_key, {}) or {}
        part_detail_payload["Commitment Part Key"] = part_key
        part_detail_payload["Commitment Owner"] = safe_text(stored_commitment.get("owner", ""))
        part_detail_payload["Commitment Date"] = safe_text(stored_commitment.get("commitment_date", ""))
        part_detail_payload["Completion Date"] = safe_text(stored_commitment.get("completion_date", ""))
        part_detail_payload["Commitment Remark"] = safe_text(stored_commitment.get("remarks", ""))
        part_detail_json = json.dumps(
            part_detail_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        part_detail_attribute = html.escape(part_detail_json, quote=True)

        html_parts.append(
            f"""
            <tr class="npi-drilldown-row npi-part-detail-clickable" data-part-key="{html.escape(part_key, quote=True)}" data-part-detail="{part_detail_attribute}" title="Single-click to select/edit commitment fields; click Save Commitment to save; double-click to open Part Detail" style="
                height:{BODY_ROW_HEIGHT}px;
                cursor:default;
            ">
            """
        )

        for col in fixed_columns:

            key = col["key"]

            heading = col[
                "heading"
            ]

            width = heading_widths.get(
                heading,
                100,
            )

            if col["type"] == "picture":

                picture = None

                if part_key:

                    picture = (
                        picture_map.get(
                            part_key
                        )
                    )

                    if picture is None:

                        try:

                            picture = (
                                get_picture_for_part(
                                    picture_map,
                                    part_key,
                                )
                            )

                        except Exception:
                            picture = None

                if picture is not None:

                    try:

                        image_b64 = (
                            image_to_base64(
                                picture
                            )
                        )

                        if image_b64:

                            cell_content = f"""
                                <img
                                    src="data:image/png;base64,{image_b64}"
                                    style="
                                        max-width:52px;
                                        max-height:60px;
                                        width:auto;
                                        height:auto;
                                        object-fit:contain;
                                        display:block;
                                        margin:auto;
                                    "
                                >
                            """

                        else:

                            cell_content = ""

                    except Exception:

                        cell_content = ""

                else:

                    cell_content = ""

            elif col["type"] == "part":

                cell_content = html_escape(
                    part_key
                )

            elif col["type"] == "date":

                cell_content = html_escape(
                    display_value(
                        row,
                        col["source"],
                        "date",
                    )
                )

            elif col["type"] == "number":

                cell_content = html_escape(
                    display_value(
                        row,
                        col["source"],
                        "number",
                    )
                )

            else:

                cell_content = html_escape(
                    display_value(
                        row,
                        col["source"],
                        "text",
                    )
                )

            sticky_style = ""

            if heading in sticky_left:

                sticky_style = f"""
                    position:sticky;
                    left:{sticky_left[heading]}px;
                    z-index:8;
                    background:{row_background};
                """

            html_parts.append(
                f"""
                <td class="{'npi-frozen-col' if heading in sticky_left else ''}"
                    title="{html_escape(
                        safe_text(
                            cell_content
                        )
                    )}"
                    style="
                        width:{width}px;
                        min-width:{width}px;
                        max-width:{width}px;
                        height:{BODY_ROW_HEIGHT}px;
                        padding:5px 6px;
                        border-right:1px solid #e0e6ee;
                        border-bottom:1px solid #e0e6ee;
                        border-left:1px solid #e0e6ee;
                        background:{row_background};
                        vertical-align:middle;
                        text-align:center;
                        box-sizing:border-box;
                        white-space:nowrap;
                        overflow:hidden;
                        text-overflow:ellipsis;
                        {sticky_style}
                    "
                >
                    {cell_content}
                </td>
                """
            )

        for col in commitment_columns:

            key = col["key"]

            heading = col[
                "heading"
            ]

            width = heading_widths.get(
                heading,
                100,
            )

            stored_commitment = commitment_lookup.get(part_key, {}) or {}
            if key == "owner":
                field_name = "owner"
                action_value = stored_commitment.get("owner") or ""
                input_type = "text"
            elif key == "commitment_date":
                field_name = "commitment_date"
                action_value = commitment_date_input_value(stored_commitment.get("commitment_date"))
                input_type = "date"
            elif key == "actual_completion":
                field_name = "completion_date"
                action_value = commitment_date_input_value(stored_commitment.get("completion_date"))
                input_type = "date"
            elif key == "remarks":
                field_name = "remarks"
                action_value = stored_commitment.get("remarks") or ""
                input_type = "text"
            else:
                field_name = ""
                action_value = ""
                input_type = "text"

            cell_content = (
                f'<input class="npi-commitment-input" type="{input_type}" '
                f'data-commitment-field="{field_name}" '
                f'aria-label="{html.escape(heading, quote=True)} for {html.escape(part_key, quote=True)}" '
                f'title="Edit {html.escape(heading, quote=True)}; click Save Commitment to save changes" '
                f'value="{html.escape(safe_text(action_value), quote=True)}" '
                'style="width:100%;min-width:0;max-width:100%;height:31px;box-sizing:border-box;'
                'padding:4px 5px;border:1px solid #d5e0eb;border-radius:5px;background:#ffffff;'
                'color:#26364a;font:500 11px Inter,Segoe UI,Arial,sans-serif;outline:none;">'
            )

            html_parts.append(
                f"""
                <td
                    title="{html_escape(
                        safe_text(
                            cell_content
                        )
                    )}"
                    style="
                        width:{width}px;
                        min-width:{width}px;
                        max-width:{width}px;
                        height:{BODY_ROW_HEIGHT}px;
                        padding:5px 6px;
                        border-right:1px solid #e0e6ee;
                        border-bottom:1px solid #e0e6ee;
                        border-left:1px solid #e0e6ee;
                        background:{row_background};
                        vertical-align:middle;
                        text-align:center;
                        box-sizing:border-box;
                        white-space:nowrap;
                        overflow:hidden;
                        text-overflow:ellipsis;
                    "
                >
                    {cell_content}
                </td>
                """
            )

        html_parts.append(
            "</tr>"
        )

    html_parts.append(
        "</tbody>"
    )

    html_parts.append(
        "</table>"
    )

    html_parts.append(
        "</div>"
    )

    html_parts.append("<script>" + _batch_a_touch_script("table", ".npi-drilldown-wrap") + "</script>")
    html_parts.append(
        """
        <script>
        (function () {
            const popupBridgeId = null;
            function npiPopupMain(config) {
                const button = document.getElementById('detail-save-commitment');
                const status = document.getElementById('detail-save-status');
                const fields = {
                    owner: document.getElementById('detail-owner'),
                    commitment_date: document.getElementById('detail-commitment-date'),
                    completion_date: document.getElementById('detail-completion-date'),
                    remarks: document.getElementById('detail-remark')
                };
                let pending = null;
                let timer = null;
                let channel = null;
                let deliveryAttempt = 0;
                let requestSequence = 0;
                function lockInputs(locked) {
                    Object.values(fields).forEach(function(input){ if(input) input.disabled = locked; });
                }
                function confirmationRetry(message) {
                    status.textContent = message;
                    button.textContent = 'Retry confirmation';
                    button.disabled = false;
                }
                function receive(data) {
                    if (!pending || !data || data.type !== 'npi_commitment_ack' ||
                        data.bridge_id !== config.bridge_id || data.popup_id !== config.popup_id ||
                        data.request_id !== pending.request_id) return;
                    clearTimeout(timer);
                    if (data.status === 'unknown' || data.status === 'pending') {
                        confirmationRetry(data.message || 'Save outcome unconfirmed. Check dashboard; retry confirmation will not write again.');
                        return;
                    }
                    if (data.status !== 'saved' && data.status !== 'error') return;
                    if (data.status === 'saved') {
                        Object.entries(data.values || {}).forEach(function(entry){
                            if (fields[entry[0]]) fields[entry[0]].value = entry[1] == null ? '' : entry[1];
                        });
                        status.textContent = 'Saved';
                    } else {
                        status.textContent = data.message || 'Save failed. Edit or retry.';
                    }
                    pending = null;
                    lockInputs(false);
                    button.textContent = 'Save Commitment';
                    button.disabled = false;
                }
                Object.values(fields).forEach(function(input){
                    if (input) input.addEventListener('input', function(){
                        if (!pending) status.textContent = 'Unsaved changes';
                    });
                });
                window.addEventListener('message', function(event){ receive(event.data); });
                try {
                    if ('BroadcastChannel' in window) {
                        channel = new BroadcastChannel('npi_commitment_channel');
                        channel.addEventListener('message', function(event){ receive(event.data); });
                    }
                } catch(e) {}
                function sendPending() {
                    button.disabled = true;
                    status.textContent = 'Waiting for server confirmation…';
                    let sent = false;
                    const message = {type:'npi_commitment_save', payload:pending, delivery_attempt:++deliveryAttempt};
                    try {
                        if (window.npiCommitmentBridge && !window.npiCommitmentBridge.closed) {
                            window.npiCommitmentBridge.postMessage(message, '*');
                            sent = true;
                        }
                    } catch(e) {}
                    if (!sent && channel) {
                        try { channel.postMessage(message); sent = true; } catch(e) {}
                    }
                    clearTimeout(timer);
                    timer = setTimeout(function(){
                        if (pending) confirmationRetry('No confirmation yet. Retry confirmation checks the same request; it does not start a new Save.');
                    }, 30000);
                    if (!sent) confirmationRetry('Unable to contact dashboard. Keep it open, then retry confirmation.');
                }
                button.addEventListener('click', function(){
                    if (button.disabled) return;
                    if (!pending) {
                        deliveryAttempt = 0;
                        pending = {
                            request_id: config.popup_id + ':' + (++requestSequence) + ':' +
                                ((window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() :
                                (Date.now().toString(36) + Math.random().toString(36).slice(2))),
                            bridge_id:config.bridge_id, popup_id:config.popup_id, part_key:config.part_key
                        };
                        Object.entries(fields).forEach(function(entry){ pending[entry[0]] = entry[1] ? entry[1].value : ''; });
                        lockInputs(true);
                    }
                    sendPending();
                });
                window.addEventListener('beforeunload', function(){
                    clearTimeout(timer);
                    if (channel) channel.close();
                });
            }
            const root = document.querySelector('.npi-drilldown-wrap') || document;
            window.addEventListener('message', function (event) {
                const data = event.data || {};
                if (data.type !== 'npi_restore_drilldown_scroll') return;
                const top = Number(data.scroll_top);
                const left = Number(data.scroll_left);
                if (Number.isFinite(top)) root.scrollTop = Math.max(0, top);
                if (Number.isFinite(left)) root.scrollLeft = Math.max(0, left);
            });
            const rows = root.querySelectorAll('tr.npi-part-detail-clickable');
            function makeRequestId() {
                return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now().toString(36) + Math.random().toString(36).slice(2));
            }
            function emitCommitmentSave(payload) {
                window.parent.postMessage({type: 'npi_commitment_save', payload: payload}, '*');
            }
            const saveButton = document.getElementById('npi-save-commitment');
            const saveStatus = document.getElementById('npi-commitment-save-status');
            let activeRow = null;
            const dirtyRows = new Map();
            function activateRow(row) {
                if (activeRow && activeRow !== row) activeRow.style.outline = '';
                activeRow = row;
                if (activeRow) activeRow.style.outline = '2px solid #0e9ab3';
            }
            function updateSaveButton() {
                const count = dirtyRows.size;
                const enabled = count > 0;
                if (saveButton) {
                    saveButton.disabled = !enabled;
                    saveButton.style.opacity = enabled ? '1' : '.55';
                }
                if (saveStatus && !enabled) saveStatus.textContent = 'Edit any number of parts, then save all changes once.';
                else if (saveStatus && enabled) saveStatus.textContent = count + ' part(s) have unsaved changes. Click Save Commitment once.';
            }
            rows.forEach((row) => {
                row.addEventListener('click', function (event) {
                    if (event.target && event.target.closest && event.target.closest('input.npi-commitment-input')) {
                        activateRow(row);
                        return;
                    }
                    activateRow(row);
                });
                const markDirty = function (event) {
                    const target = event.target;
                    if (!target || !target.matches || !target.matches('input.npi-commitment-input[data-commitment-field]')) return;
                    activateRow(row);
                    const key = String(row.getAttribute('data-part-key') || '').trim();
                    if (key) dirtyRows.set(key, row);
                    target.style.borderColor = '#0e9ab3';
                    updateSaveButton();
                };
                row.addEventListener('input', markDirty);
                row.addEventListener('change', markDirty);
                row.addEventListener('dblclick', function () {
                    let detail;
                    try { detail = JSON.parse(this.getAttribute('data-part-detail') || '{}'); }
                    catch (e) { return; }
                    const popup = window.open('', '_blank', 'width=1180,height=900,resizable=yes,scrollbars=yes');
                    if (!popup) {
                        alert('Part Detail pop-up was blocked by your browser. Please allow pop-ups for this app and click the row again.');
                        return;
                    }
                    const esc = (value) => String(value == null || value === '' ? '—' : value)
                        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
                    const field = (label, value, extra='') => `<div class="field ${extra}"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
                    const routing = Array.isArray(detail['Process Routing']) ? detail['Process Routing'] : [];
                    const parseDate = (value) => {
                        const s = String(value == null ? '' : value).trim();
                        if (!s || s === '—') return null;
                        let m = s.match(/^(\\d{4})[-/.](\\d{1,2})[-/.](\\d{1,2})/);
                        if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
                        m = s.match(/^(\\d{1,2})[-/.](\\d{1,2})[-/.](\\d{4})/);
                        if (m) return new Date(Number(m[3]), Number(m[2]) - 1, Number(m[1]));
                        const d = new Date(s);
                        return Number.isNaN(d.getTime()) ? null : new Date(d.getFullYear(), d.getMonth(), d.getDate());
                    };
                    const routingRows = routing.length ? routing.map((r, i) => {
                        const plan = parseDate(r['Plan Start Date']);
                        const actual = parseDate(r['Actual Start Date']);
                        const late = !!(plan && actual && actual > plan);
                        return `<tr><td class="step">${i + 1}</td><td>${esc(r['Operation'])}</td><td>${esc(r['Plan Start Date'])}</td><td class="${late ? 'actual-late' : ''}">${esc(r['Actual Start Date'])}</td></tr>`;
                    }).join('') : '<tr><td colspan="4" class="empty">No process routing records available in the loaded data.</td></tr>';
                    const picture = detail.Picture ? `<img class="part-picture" src="${detail.Picture}" alt="Part picture">` : '<div class="picture-placeholder">No picture available</div>';
                    popup.document.open();
                    popup.document.write(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Part Detail — ${esc(detail['Part Number'])}</title><style>
                        *{box-sizing:border-box} html,body{height:100%;overflow:hidden} body{margin:0;background:#eef3f8;color:#1e293b;font-family:Inter,Segoe UI,Arial,sans-serif;padding:14px}
                        .page{max-width:1600px;height:100%;margin:0 auto;display:flex;flex-direction:column;min-height:0}.top{display:flex;flex-shrink:0;justify-content:space-between;align-items:center;gap:20px;margin-bottom:12px;padding:12px 18px;background:linear-gradient(115deg,#172b46,#234b68);border-radius:16px;color:#fff;box-shadow:0 8px 24px rgba(22,43,70,.16)}
                        .eyebrow{font-size:10px;font-weight:800;letter-spacing:1.6px;color:#79d4e4;text-transform:uppercase;margin-bottom:7px}.title{font-size:27px;font-weight:800;line-height:1.2;margin:0;overflow-wrap:anywhere;color:#fff}.subtitle{color:#d3e0ec;font-size:12px;margin-top:7px}
                        .close{border:1px solid rgba(255,255,255,.3);background:rgba(255,255,255,.1);color:#fff;border-radius:9px;padding:9px 13px;font-weight:700;cursor:pointer;white-space:nowrap}.close:hover{background:rgba(255,255,255,.2)}.detail-layout{display:grid;grid-template-columns:minmax(390px,1.04fr) minmax(470px,1fr);gap:14px;align-items:stretch;flex:1;min-height:0}.right-column{display:flex;flex-direction:column;gap:10px;min-width:0;min-height:0;overflow:hidden}.left-column{display:flex;flex-direction:column;gap:12px;min-width:0;min-height:0;overflow:hidden}.left-column>.card{flex:1;min-height:0;overflow:hidden}.detail-layout>.card{margin-bottom:0;min-width:0}.picture-card,.card{background:#fff;border:1px solid #dfe7f0;border-radius:14px;box-shadow:0 5px 18px rgba(15,23,42,.045)}
                        .picture-card{min-height:170px;padding:10px;display:grid;grid-template-columns:minmax(0,1fr) minmax(145px,.72fr);gap:12px;align-items:stretch;background:linear-gradient(145deg,#fff,#f5f9fc);overflow:hidden}.picture-stage{min-width:0;display:flex;align-items:center;justify-content:center;border-radius:10px;background:#fff;border:1px solid #edf1f6;padding:8px}.part-picture{display:block;max-width:100%;max-height:145px;object-fit:contain}.picture-placeholder{min-height:140px;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:13px;text-align:center}.picture-stats{display:grid;grid-template-rows:1fr 1fr;gap:10px;min-width:0}.stat-tile{display:flex;flex-direction:column;justify-content:center;padding:12px 13px;border-radius:11px;background:linear-gradient(140deg,#20354f,#2c5873);color:#fff;min-width:0}.stat-tile.qty{background:linear-gradient(140deg,#087f9b,#10a4b7)}.stat-label{font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#d5e7ef;margin-bottom:8px}.stat-value{font-size:clamp(23px,2.5vw,34px);font-weight:850;line-height:1.05;overflow-wrap:anywhere;color:#fff}.process-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:0 0 7px}.process-stat{padding:6px 8px;border-radius:10px;border:1px solid #e0eaf1;background:#f5f9fc}.process-stat .label{margin-bottom:3px}.process-stat .value{font-size:15px;font-weight:800;color:#20354f}
                        .card{padding:16px;margin-bottom:0}.section-title{display:flex;align-items:center;gap:9px;font-size:14px;font-weight:800;color:#20354f;margin:0 0 8px}.section-title:before{content:'';width:4px;height:18px;border-radius:4px;background:#0e9ab3}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.field{padding:9px 10px;border:1px solid #e8eef5;border-radius:9px;background:#f8fafc;min-width:0}.label{font-size:9px;text-transform:uppercase;letter-spacing:.7px;color:#718096;font-weight:800;margin-bottom:5px}.value{font-size:12px;font-weight:650;color:#26364a;overflow-wrap:anywhere;min-height:15px}.process-card{padding:10px 11px;position:relative;display:flex;flex-direction:column;flex:1 1 auto;min-height:0;overflow:hidden}.process-meta{font-size:10px;color:#718096;margin:-5px 0 5px}.routing-wrap{flex:1 1 auto;height:0;max-height:none;min-height:0;overflow:auto;border:1px solid #e1e8f0;border-radius:10px;scrollbar-width:thin;scrollbar-color:#c2cedb #f4f7fa}.routing{width:100%;border-collapse:separate;border-spacing:0;font-size:11px;overflow:hidden}.routing th{position:sticky;top:0;z-index:2;background:#20354f;color:#fff;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:.45px;padding:6px 7px;white-space:normal}.routing td{padding:5px 7px;border-top:1px solid #e8edf3;background:#fff;vertical-align:top}.routing tr:nth-child(even) td{background:#f7f9fc}.routing tbody tr:hover td{background:#eaf6fa}.routing .step{width:38px;color:#0e8aa3;font-weight:800;text-align:center}.routing td.actual-late{background:#fee2e2!important;color:#b91c1c;font-weight:800;box-shadow:inset 3px 0 0 #dc2626}.empty{text-align:center;color:#94a3b8;padding:24px!important}.footer{font-size:9px;color:#8190a3;text-align:right;margin-top:5px;flex-shrink:0}@media(max-width:980px){body{padding:10px}.detail-layout{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.process-card{position:relative}.routing-wrap{max-height:none;min-height:0}.top{padding:10px 13px;margin-bottom:9px}.title{font-size:22px}.card{padding:12px}}@media(max-width:650px){body{padding:8px;overflow:auto}.page{height:auto;min-height:100vh;overflow:visible}.detail-layout{display:flex;flex-direction:column;gap:10px;min-height:0;overflow:visible}.left-column,.right-column{overflow:visible;min-height:0}.left-column>.card{flex:0 0 auto;overflow:visible}.picture-card{grid-template-columns:1fr}.picture-stage{min-height:100px}.picture-stats{grid-template-columns:1fr 1fr;grid-template-rows:auto}.stat-value{font-size:20px}.grid{grid-template-columns:1fr}.process-stats{grid-template-columns:1fr}.process-card{flex:0 0 auto;min-height:420px;overflow:visible}.routing-wrap{height:auto;max-height:55vh;min-height:220px;overflow:auto;-webkit-overflow-scrolling:touch}.action-form{grid-template-columns:1fr}.action-form .remark-field{grid-column:auto}.routing th,.routing td{padding:7px 6px;font-size:10px}.section-title{font-size:13px}.card,.process-card{padding:9px}.top{align-items:flex-start;gap:8px}.top .close{min-height:40px;flex-shrink:0}}
                        @media(max-width:520px){.picture-card{grid-template-columns:1fr}.picture-stage{min-height:150px}.picture-stats{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:auto}.stat-value{font-size:25px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.card,.process-card{padding:12px}.routing th,.routing td{padding:8px 7px}}
                    </style></head><body><main class="page"><header class="top"><div><div class="eyebrow">NPI Control Tower · Part Profile</div><h1 class="title">${esc(detail['Part Number'])}</h1><div class="subtitle">Purchase Order: ${esc(detail['PO Number'])} &nbsp;·&nbsp; Job: ${esc(detail['Job Number'])}</div></div><button class="close" onclick="window.close()">✕ Close</button></header>
                    <section class="detail-layout"><div class="left-column"><div class="picture-card"><div class="picture-stage">${picture}</div><div class="picture-stats"><div class="stat-tile qty"><div class="stat-label">Qty</div><div class="stat-value">${esc(detail.Qty)}</div></div><div class="stat-tile"><div class="stat-label">Engineer</div><div class="stat-value" style="font-size:clamp(17px,1.8vw,25px)">${esc(detail.Engineer)}</div></div></div></div><section class="card"><h2 class="section-title">Part Informations</h2><div class="grid">${field('PO Number',detail['PO Number'])}${field('Order Date',detail['Order Date'])}${field('Exwork Date',detail['Exwork Date'])}${field('Job Number',detail['Job Number'])}${field('Nesting Number',detail['Nesting Number'] === '' || detail['Nesting Number'] == null ? '' : (Number.isFinite(Number(detail['Nesting Number'])) ? String(Math.trunc(Number(detail['Nesting Number']))) : detail['Nesting Number']))}${field('Material',detail.Material)}${field('Plating',detail.Plating)}${field('Finishing',detail.Finishing)}${field('Subpart 2D Rev',detail['Subpart 2D Rev'])}${field('Subpart 3D Rev',detail['Subpart 3D Rev'])}${field('Subpart KK Rev',detail['Subpart KK Rev'])}</div></section></div>
                    <div class="right-column"><section class="card process-card"><h2 class="section-title">Process</h2><div class="process-meta">${routing.length} process step(s) · Scroll within this panel to view all steps</div><div class="process-stats"><div class="process-stat"><div class="label">Delay Days</div><div class="value">${esc(detail['Delay Days'])}</div></div><div class="process-stat"><div class="label">Aging</div><div class="value">${esc(detail.Aging)}</div></div><div class="process-stat"><div class="label">First Process Plan Date</div><div class="value">${esc(detail['First Process Plan Date'])}</div></div></div><div class="routing-wrap"><table class="routing"><thead><tr><th style="width:42px">Step</th><th style="width:31%">Operation</th><th>Plan Start Date</th><th>Actual Start Date</th></tr></thead><tbody>${routingRows}</tbody></table></div><div class="footer">Actual Start Date turns red when it is later than Plan Start Date</div></section><section class="card action-card"><h2 class="section-title">Commitment & Remarks</h2><div class="action-form"><label>Owner<input type="text" id="detail-owner" placeholder="Enter owner" value="${esc(detail['Commitment Owner'] || '')}"></label><label>Commitment Date<input type="date" id="detail-commitment-date" value="${esc(detail['Commitment Date'] || '')}"></label><label>Completion Date<input type="date" id="detail-completion-date" value="${esc(detail['Completion Date'] || '')}"></label><label class="remark-field">Remark<textarea id="detail-remark" rows="3" placeholder="Enter remark">${esc(detail['Commitment Remark'] || '')}</textarea></label></div><div class="action-actions"><button id="detail-save-commitment" class="save-commitment">Save Commitment</button><span id="detail-save-status" role="status"></span></div><div class="action-note">Saved records are shared with other approved users. Previous values are retained for audit.</div></section></div></section></main><style>.action-card{margin-top:0;flex:0 0 auto;overflow:hidden;padding:12px}.action-form{display:grid;grid-template-columns:1fr 1fr;gap:7px}.action-form label{display:flex;flex-direction:column;gap:6px;font-size:10px;font-weight:800;letter-spacing:.6px;text-transform:uppercase;color:#718096}.action-form input,.action-form textarea{width:100%;font:500 12px Inter,Segoe UI,Arial,sans-serif;color:#26364a;background:#f8fafc;border:1px solid #dbe5ef;border-radius:8px;padding:7px 8px;outline:none}.action-form input:focus,.action-form textarea:focus{border-color:#0e9ab3;box-shadow:0 0 0 3px rgba(14,154,179,.12)}.action-form .remark-field{grid-column:1/-1}.action-form textarea{resize:none;min-height:46px;height:46px}.action-note{margin-top:6px;font-size:10px;color:#8190a3}.action-actions{display:flex;align-items:center;gap:12px;margin-top:8px}.save-commitment{border:0;border-radius:9px;background:#087f8c;color:#fff;padding:10px 16px;font-weight:800;cursor:pointer}.save-commitment:disabled{opacity:.65;cursor:wait}#detail-save-status{font-size:11px;color:#087f8c;font-weight:700}@media(max-width:520px){.process-stats{grid-template-columns:1fr}.action-form{grid-template-columns:1fr}}</style></body></html>`);
                    popup.document.close();
                    popup.npiCommitmentBridge = window.parent;
                    const popupScript = popup.document.createElement('script');
                    popupScript.textContent = '(' + npiPopupMain.toString() + ')(' + JSON.stringify({
                        bridge_id: popupBridgeId,
                        popup_id: makeRequestId(),
                        part_key: String(detail['Commitment Part Key'] || detail['Part Number'] || '').trim()
                    }) + ');';
                    popup.document.body.appendChild(popupScript);
                });
            });
            if (saveButton) saveButton.addEventListener('click', function () {
                if (!dirtyRows.size) return;
                const records = [];
                dirtyRows.forEach((row, key) => {
                    const values = {};
                    row.querySelectorAll('input.npi-commitment-input[data-commitment-field]').forEach((input) => {
                        values[input.getAttribute('data-commitment-field')] = input.value;
                    });
                    const record = {
                        part_key: key,
                        owner: Object.prototype.hasOwnProperty.call(values, 'owner') ? values.owner : '',
                        commitment_date: Object.prototype.hasOwnProperty.call(values, 'commitment_date') ? values.commitment_date : '',
                        completion_date: Object.prototype.hasOwnProperty.call(values, 'completion_date') ? values.completion_date : '',
                        remarks: Object.prototype.hasOwnProperty.call(values, 'remarks') ? values.remarks : ''
                    };
                    // Keep the popup payload in sync without reloading the drilldown iframe.
                    try {
                        const detail = JSON.parse(row.getAttribute('data-part-detail') || '{}');
                        detail['Commitment Owner'] = record.owner;
                        detail['Commitment Date'] = record.commitment_date;
                        detail['Completion Date'] = record.completion_date;
                        detail['Commitment Remark'] = record.remarks;
                        row.setAttribute('data-part-detail', JSON.stringify(detail));
                    } catch (e) {}
                    records.push(record);
                });
                const payload = {
                    type: 'npi_commitment_save',
                    request_id: makeRequestId(),
                    records: records,
                    scroll_top: root.scrollTop || 0,
                    scroll_left: root.scrollLeft || 0
                };
                saveButton.disabled = true;
                saveButton.style.opacity = '.55';
                if (saveStatus) saveStatus.textContent = 'Saving all changes for ' + records.length + ' part(s)…';
                emitCommitmentSave(payload);
                dirtyRows.clear();
            });
            updateSaveButton();
        })();
        </script>
        """.replace("const popupBridgeId = null;", "const popupBridgeId = " + json.dumps(popup_bridge_id) + ";")
    )

    component_event = _npi_drilldown_component(
        html_content=('<!doctype html><html><head><meta charset="utf-8">'
                      '<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>'
                      + "".join(html_parts) + '</body></html>'),
        height=760,
        key="npi_drilldown_commitment_bridge",
        # Preserve scroll only while the displayed page and part order are unchanged.
        viewport_key=hashlib.sha256(json.dumps(
            [str(customer_display), title, selected_current_process, page,
             [safe_text(value) for value in page_df[part_col].tolist()]
             if part_col and part_col in page_df.columns else []],
            ensure_ascii=True,
        ).encode("utf-8")).hexdigest(),
        popup_bridge_id=popup_bridge_id,
        popup_results=list(popup_requests.values()),
        default=None,
    )
    if isinstance(component_event, dict) and component_event.get("type") == "npi_commitment_save":
        event_payload = component_event.get("payload")
        if isinstance(event_payload, dict):
            event_request_id = safe_text(event_payload.get("request_id"))
            if event_payload.get("popup_id"):
                # One immutable outcome per popup request, including indeterminate RPC results.
                # Never replay a write after a timeout or a later duplicate delivery.
                if (event_request_id and event_payload.get("bridge_id") == popup_bridge_id
                        and event_request_id not in popup_requests):
                    popup_part = safe_text(event_payload.get("part_key")).strip()
                    popup_changes = {
                        "owner": safe_text(event_payload.get("owner")).strip() or None,
                        "commitment_date": safe_text(event_payload.get("commitment_date")).strip() or None,
                        "completion_date": safe_text(event_payload.get("completion_date")).strip() or None,
                        "remarks": safe_text(event_payload.get("remarks")),
                    }
                    outcome = {
                        "type": "npi_commitment_ack", "bridge_id": popup_bridge_id,
                        "popup_id": safe_text(event_payload.get("popup_id")),
                        "request_id": event_request_id, "status": "unknown",
                        "message": "Save outcome unconfirmed. Check dashboard before another Save; retry confirmation will not write again.",
                    }
                    popup_requests[event_request_id] = outcome
                    if not popup_part:
                        outcome.update(status="error", message="Part key is empty. Nothing was saved.")
                    else:
                        try:
                            save_supabase_commitment_patch(popup_part, popup_changes, user_id=current_user_id)
                        except PermissionError:
                            # The secure store raises this before its RPC, so a fresh retry is safe.
                            outcome.update(status="error", message="Access denied. Nothing was saved. Check login/approval, then retry.")
                        except Exception:
                            # A transport failure can occur after DB commit. Do not claim failure,
                            # mark success, or retry the RPC without database idempotency support.
                            pass
                        else:
                            outcome.update(status="saved", message="Saved", values=popup_changes)
                            st.session_state["_npi_commitment_success_message"] = "Commitment saved for 1 part(s)."
                    st.rerun()
            elif event_request_id and event_request_id != st.session_state.get("_npi_last_commitment_request_id"):
                raw_records = event_payload.get("records")
                if not isinstance(raw_records, list):
                    raw_records = [event_payload]
                records = [record for record in raw_records if isinstance(record, dict)]
                saved_parts = []
                try:
                    for record in records:
                        part_key_to_save = safe_text(record.get("part_key")).strip()
                        if not part_key_to_save:
                            continue
                        save_supabase_commitment_patch(
                            part_key_to_save,
                            {
                                "owner": safe_text(record.get("owner")),
                                "commitment_date": safe_text(record.get("commitment_date")) or None,
                                "completion_date": safe_text(record.get("completion_date")) or None,
                                "remarks": safe_text(record.get("remarks")),
                            },
                            user_id=current_user_id,
                        )
                        saved_parts.append(part_key_to_save)
                    if not saved_parts:
                        raise ValueError("No valid part records were included in the save request.")
                    st.session_state["_npi_last_commitment_request_id"] = event_request_id
                    st.session_state["_npi_commitment_success_message"] = (
                        f"Commitment saved for {len(saved_parts)} part(s)."
                    )
                except Exception as commitment_error:
                    st.session_state["_npi_last_commitment_request_id"] = event_request_id
                    st.error(
                        f"Unable to save commitment batch. Saved before error: "
                        f"{', '.join(saved_parts) or 'none'}. Error: {commitment_error}"
                    )
                else:
                    # Read Latest and rebuild component HTML after successful persistence.
                    # The recorded request ID prevents replay from saving twice.
                    st.rerun()


# ============================================================
# CONTROL TOWER MODULE
# ============================================================

def render_control_tower():

    # ========================================================
    # EXECUTIVE STATUS CLICK HANDLER
    # ========================================================

    executive_jump = (
        st.query_params.get(
            "npi_exec"
        )
    )

    if executive_jump:

        valid_classifications = {
            item["classification"]
            for item in EXECUTIVE_STATUS
        }

        if (
            executive_jump == "ALL"
            or executive_jump in valid_classifications
        ):

            st.session_state[
                "selected_classification"
            ] = executive_jump

            # Consume this one-time URL event immediately. Without
            # this, a stale npi_exec value can replay on later reruns.
            if "npi_exec" in st.query_params:
                del st.query_params["npi_exec"]

            st.session_state[
                "drilldown_jump_id"
            ] = int(
                st.session_state.get(
                    "drilldown_jump_id",
                    0,
                )
            ) + 1

        elif "npi_exec" in st.query_params:
            # Also consume invalid values rather than repeatedly reading them.
            del st.query_params["npi_exec"]

    # --------------------------------------------------------
    # CONTROL TOWER TOP HEADER STYLING
    # --------------------------------------------------------

    st.markdown(
        """
        <style>
        [data-testid="stMainBlockContainer"] {
            padding-top: 1.5rem !important;
        }
        .npi-control-tower-top-title {
            font-size: 30px;
            line-height: 1.2;
            font-weight: 800;
            white-space: nowrap;
            margin: 0;
            padding: 0;
        }
        .npi-control-tower-top-meta {
            font-size: 15px;
            line-height: 1.2;
            white-space: nowrap;
            margin: 0;
            padding: 0;
        }
        div[data-testid="stSelectbox"] label {
            display: none;
        }
        @media (max-width: 760px) {
            [data-testid="stMainBlockContainer"] { padding: 0.65rem 0.65rem 2rem !important; }
            .npi-control-tower-top-title { font-size: 22px !important; white-space: normal !important; overflow-wrap: anywhere; }
            .npi-control-tower-top-meta { font-size: 12px !important; white-space: normal !important; }
            [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.5rem !important; }
            [data-testid="stHorizontalBlock"] > [data-testid="column"] { min-width: min(100%, 260px) !important; flex: 1 1 100% !important; width: 100% !important; }
            [data-testid="stButton"] button, [data-testid="stDownloadButton"] button, [data-testid="stSelectbox"] div[data-baseweb="select"] { min-height: 42px !important; }
            [data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea, [data-testid="stDateInput"] input { font-size: 16px !important; }
            [data-testid="stFileUploader"] section { padding: 0.65rem !important; }
        }
        @media (max-width: 420px) {
            .npi-control-tower-top-title { font-size: 20px !important; }
            [data-testid="stMainBlockContainer"] { padding-left: 0.45rem !important; padding-right: 0.45rem !important; }
        }
        
div.st-key-npi_main_header { container-type:inline-size; container-name:npiMain; min-width:0; max-width:100%; }
div.st-key-npi_main_header [data-testid="stHorizontalBlock"] { display:grid !important; grid-template-columns:minmax(0,1fr) minmax(180px,260px); gap:16px !important; align-items:center; }
div.st-key-npi_main_header :is([data-testid="column"],[data-testid="stColumn"]) { min-width:0 !important; width:auto !important; max-width:100% !important; flex:none !important; }
div.st-key-npi_main_header [data-testid="stVerticalBlock"] { gap:4px; }
div.st-key-npi_main_header .npi-control-tower-top-title { display:block; font-size:28px; line-height:1.2; color:#20354f; letter-spacing:-.5px; white-space:normal; overflow-wrap:anywhere; }
div.st-key-npi_main_header .npi-control-tower-top-meta { display:block; font-size:13px; color:#64748b; line-height:1.4; white-space:normal; margin-top:5px; }
div.st-key-npi_main_header .npi-header-label { font-size:12px; font-weight:600; color:#64748b; margin:0; }
div.st-key-npi_main_header [data-testid="stSelectbox"] { min-width:0; max-width:100%; }
@container npiMain (max-width:650px) {
 div.st-key-npi_main_header [data-testid="stHorizontalBlock"] { grid-template-columns:minmax(0,1fr); gap:10px !important; }
 div.st-key-npi_main_header .npi-control-tower-top-title { font-size:24px !important; }
}
</style>
        """,
        unsafe_allow_html=True,
    )

    # --------------------------------------------------------
    # PAGE TOP ANCHOR
    # --------------------------------------------------------

    st.markdown(
        """
        <div
            id="npi-page-top-anchor"
            style="
                position:relative;
                height:1px;
                margin:0;
                padding:0;
                scroll-margin-top:0;
            "
        ></div>
        """,
        unsafe_allow_html=True,
    )

    # --------------------------------------------------------
    # CUSTOMER FILTER
    # --------------------------------------------------------

    customer_list = get_customer_list(
        st.session_state.unique_df
    )

    current_customer = (
        st.session_state.selected_customer
    )

    if current_customer not in customer_list:
        current_customer = "ALL"

    # Scoped responsive native header; keep the existing Customer widget and key.
    with st.container(key="npi_main_header"):
        header_title_col, header_customer_col = st.columns(
            [4, 1.5],
            vertical_alignment="center",
            gap="small",
        )

        with header_title_col:
            st.markdown(
                f"""
                <div class="npi-main-title-date">
                    <span class="npi-control-tower-top-title">NPI CONTROL TOWER</span>
                    <span class="npi-control-tower-top-meta">Control Date · {control_date.strftime('%d/%m/%Y')}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with header_customer_col:
            st.markdown('<p class="npi-header-label">Customer</p>', unsafe_allow_html=True)
            selected_customer_ui = st.selectbox(
                "Customer",
                customer_list,
                index=customer_list.index(current_customer),
                key="control_tower_customer",
                label_visibility="collapsed",
            )

    if (
        selected_customer_ui
        != st.session_state.selected_customer
    ):

        st.session_state.selected_customer = (
            selected_customer_ui
        )

        # Customer is an independent filter dimension: changing it
        # must clear any previous classification selection.
        st.session_state.selected_classification = "ALL"
        st.session_state["selected_delay_bucket"] = ""
        # A hotspot / commitment selection belongs to the previous customer's context.
        # Clear them so the new customer starts with ALL unique parts.
        st.session_state["selected_hotspot"] = ""
        st.session_state["selected_commitment_status"] = ""
        st.session_state["drilldown_current_process_filter"] = "ALL"
        st.session_state["enquiry_search_active"] = False
        st.session_state["selected_enquiry_parts"] = []

        # Save the updated customer context before the explicit rerun.
        _save_persistent_state()

        # Discard any stale donut URL event before rerunning.
        if "npi_exec" in st.query_params:
            del st.query_params["npi_exec"]

        # Start the new customer's Drilldown at page 1, but do not trigger
        # the global auto-scroll controller. Keep the user's current viewport.
        for page_key in list(st.session_state.keys()):
            if str(page_key).startswith("drilldown_page_state_"):
                st.session_state[page_key] = 1

        # Save updated customer/filter context before the explicit rerun.
        _save_persistent_state()
        st.rerun()

    # IMPORTANT:
    # Use the current UI customer immediately.

    tower_unique = apply_customer_filter(
        st.session_state.unique_df,
        selected_customer_ui,
    )

    tower_calc = apply_customer_filter(
        st.session_state.calc_df,
        selected_customer_ui,
    )

    # --------------------------------------------------------
    # TOP-01 + PRODUCTION HOTSPOT FLOATING WIDGET
    # --------------------------------------------------------

    delay_bucket_summary = build_delay_bucket_summary(tower_unique)
    production_df = tower_unique[
        tower_unique["CALC_CLASSIFICATION"].fillna("").astype(str).str.strip().eq("RED - PRODUCTION")
    ].copy()
    hotspot = build_production_hotspot(production_df)

    # Executive Status, Delay Bucket and Production Hotspot share one floating/resizable stage.
    render_executive_status(
        tower_unique,
        delay_bucket_summary=delay_bucket_summary,
        hotspot_df=hotspot,
    )

    # The floating dashboard now ends at a draggable boundary. Do not add
    # legacy Management Action dividers or whitespace before the Drilldown.

    # --------------------------------------------------------
    # DRILLDOWN
    # --------------------------------------------------------

    selected_classification = (
        st.session_state.selected_classification
    )

    drill_df = (
        apply_classification_filter(
            tower_unique,
            selected_classification,
        )
    )

    selected_commitment_status = st.session_state.get("selected_commitment_status", "")
    if selected_commitment_status:
        commitment_status_map = build_delay_commitment_status_map(
            tower_unique,
            st.session_state.get("actions_df"),
            control_date,
        )
        matching_commitment_parts = {
            part_key for part_key, status in commitment_status_map.items()
            if status == selected_commitment_status
        }
        commitment_part_col = first_existing_column(
            drill_df,
            ["PART NUMBER", "Part Number", "Subpart Part Num", "PART", "Part"],
        )
        if commitment_part_col and matching_commitment_parts:
            drill_df = drill_df[
                drill_df[commitment_part_col].map(normalize_part_number).isin(
                    matching_commitment_parts
                )
            ].copy()
        else:
            drill_df = drill_df.iloc[0:0].copy()
        drill_title = f"DELAY COMMITMENT: {selected_commitment_status} ({len(drill_df):,})"
    else:
        enquiry_search_active = bool(
            st.session_state.get("enquiry_search_active", False)
        )
        if enquiry_search_active:
            selected_enquiry_parts = set(
                st.session_state.get("selected_enquiry_parts", [])
            )
            enquiry_part_col = first_existing_column(
                drill_df,
                ["PART NUMBER", "Part Number", "Subpart Part Num", "PART", "Part"],
            )
            if enquiry_part_col and selected_enquiry_parts:
                drill_df = drill_df[
                    drill_df[enquiry_part_col].map(normalize_part_number).isin(
                        selected_enquiry_parts
                    )
                ].copy()
            else:
                drill_df = drill_df.iloc[0:0].copy()
            drill_title = f"ENQUIRY RESULTS ({len(drill_df):,})"
        else:
            selected_delay_bucket = st.session_state.get("selected_delay_bucket", "")
            if selected_delay_bucket:
                bucket_col = first_existing_column(
                    drill_df,
                    ["CALC_PRODUCTION_DELAY_BUCKET", "Production Delay Bucket", "Part Delay Days Bucket"],
                )
                if bucket_col is not None:
                    def _normalize_delay_bucket(value):
                        value = str(value).strip().lower()
                        for dash in ("–", "—", "−"):
                            value = value.replace(dash, "-")
                        return " ".join(value.split())

                    selected_norm = _normalize_delay_bucket(selected_delay_bucket)
                    drill_df = drill_df[
                        drill_df[bucket_col].astype(str).map(_normalize_delay_bucket).eq(selected_norm)
                    ].copy()
                    # A delay bucket contains RED - PRODUCTION parts only.
                    class_col = first_existing_column(
                        drill_df, ["CALC_CLASSIFICATION", "CLASSIFICATION"]
                    )
                    if class_col is not None:
                        drill_df = drill_df[
                            drill_df[class_col].fillna("").astype(str).str.strip().str.upper()
                            == "RED - PRODUCTION"
                        ].copy()
                drill_title = f"{selected_delay_bucket} ({len(drill_df):,})"
            elif st.session_state.get("selected_hotspot", ""):
                selected_hotspot = st.session_state["selected_hotspot"]
                current_col = get_current_column(drill_df)
                if current_col is not None:
                    drill_df = drill_df[
                        drill_df[current_col].fillna("").astype(str).str.strip().eq(selected_hotspot)
                    ].copy()
                drill_title = f"HOTSPOT: {selected_hotspot} ({len(drill_df):,})"
            elif selected_classification == "ALL":
                drill_title = f"ALL UNIQUE PARTS ({len(drill_df):,})"
            else:
                drill_title = f"{selected_classification} ({len(drill_df):,})"

    render_drilldown(
        drill_df,
        drill_title,
    )


# ============================================================
# DATA VALIDATION
# ============================================================

def render_data_validation():

    render_title(
        "DATA VALIDATION",
        "Virgin Epicor Web BAQ raw data layer.",
    )

    cols = st.columns(3)

    cols[0].metric(
        "RAW ROWS",
        f"{len(raw_df):,}",
    )

    cols[1].metric(
        "COLUMNS",
        f"{len(raw_df.columns):,}",
    )

    cols[2].metric(
        "PICTURE MAPPINGS",
        f"{len(picture_map):,}",
    )

    st.divider()

    st.markdown(
        "### RAW DATA PREVIEW"
    )

    st.dataframe(
        raw_df.head(100),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()

    st.markdown(
        "### PICTURE SUMMARY"
    )

    try:

        picture_summary = (
            get_picture_summary(
                picture_map
            )
        )

        if isinstance(
            picture_summary,
            pd.DataFrame,
        ):

            st.dataframe(
                picture_summary,
                use_container_width=True,
                hide_index=True,
            )

        else:

            st.write(
                picture_summary
            )

    except Exception:

        st.info(
            f"Picture mappings: "
            f"{len(picture_map):,}"
        )


# ============================================================
# UNIQUE PART VALIDATION
# ============================================================

def render_unique_validation():

    render_title(
        "UNIQUE-PART VALIDATION",
        (
            "Column E = authoritative "
            "full part identity."
        ),
    )

    calc_rows = len(calc_df)

    unique_rows = len(
        unique_df
    )

    duplicates = (
        calc_rows
        - unique_rows
    )

    cols = st.columns(3)

    cols[0].metric(
        "CALCULATION ROWS",
        f"{calc_rows:,}",
    )

    cols[1].metric(
        "UNIQUE PARTS",
        f"{unique_rows:,}",
    )

    cols[2].metric(
        "DUPLICATES REMOVED",
        f"{duplicates:,}",
    )

    st.divider()

    st.markdown(
        "### UNIQUE PART DATASET"
    )

    st.dataframe(
        unique_df,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# CALCULATION LAYER
# ============================================================

def render_calculation_layer():

    render_title(
        "CALCULATION LAYER",
        (
            "Derived NPI status before "
            "Control Tower presentation."
        ),
    )

    engineering = count_classification(
        unique_df,
        "RED - ENGINEERING",
    )

    process = count_classification(
        unique_df,
        "RED - PROCESS",
    )

    production = count_classification(
        unique_df,
        "RED - PRODUCTION",
    )

    cols = st.columns(4)

    cols[0].metric(
        "CALCULATION ROWS",
        f"{len(calc_df):,}",
    )

    cols[1].metric(
        "UNIQUE PARTS",
        f"{len(unique_df):,}",
    )

    cols[2].metric(
        "PRODUCTION",
        f"{production:,}",
    )

    cols[3].metric(
        "ENGINEERING + PROCESS",
        f"{engineering + process:,}",
    )

    st.divider()

    st.markdown(
        "### CLASSIFICATION SUMMARY"
    )

    if (
        "CALC_CLASSIFICATION"
        in unique_df.columns
    ):

        summary = (
            unique_df[
                "CALC_CLASSIFICATION"
            ]
            .value_counts()
            .rename_axis(
                "CLASSIFICATION"
            )
            .reset_index(
                name="UNIQUE PARTS"
            )
        )

        st.dataframe(
            summary,
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    st.markdown(
        "### CALCULATION DATA"
    )

    st.dataframe(
        calc_df,
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# PERSISTENT UI CONTROLLER
# ============================================================

render_ui_controller(
    active=(
        module == "Control Tower"
    ),
    jump_id=st.session_state.get(
        "drilldown_jump_id",
        0,
    ),
)


# ============================================================
# MODULE ROUTER
# ============================================================

if module == "Control Tower":

    render_control_tower()

elif module == "Data Validation":

    render_data_validation()

elif module == "Unique-Part Validation":

    render_unique_validation()

elif module == "Calculation Layer":

    render_calculation_layer()


if st.session_state.get("show_enquiry_missing_dialog", False):
    _render_enquiry_missing_dialog()


# Preserve this browser's loaded BAQ, customer and UI filters across a temporary
# Streamlit WebSocket/session reset while the app process remains available.
_save_persistent_state()
