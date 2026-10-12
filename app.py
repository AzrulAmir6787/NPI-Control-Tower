import base64
import hmac
import html
import io
import math

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
# LOGIN / ACCESS GATE
# ============================================================

try:
    LOGIN_USERNAME = str(
        st.secrets["npi_login"]["username"]
    )
    LOGIN_PASSWORD = str(
        st.secrets["npi_login"]["password"]
    )

except (FileNotFoundError, KeyError):
    st.error(
        "Login credentials are not configured. "
        "Please contact the app owner."
    )
    st.stop()


if "npi_authenticated" not in st.session_state:
    st.session_state["npi_authenticated"] = False


if not st.session_state["npi_authenticated"]:

    st.title("NPI Control Tower")
    st.caption("Please sign in to continue.")

    with st.form("npi_login_form"):

        username = st.text_input("Username")

        password = st.text_input(
            "Password",
            type="password",
        )

        submitted = st.form_submit_button("Log in")

    if submitted:

        username_valid = hmac.compare_digest(
            username.encode("utf-8"),
            LOGIN_USERNAME.encode("utf-8"),
        )

        password_valid = hmac.compare_digest(
            password.encode("utf-8"),
            LOGIN_PASSWORD.encode("utf-8"),
        )

        if username_valid and password_valid:
            st.session_state["npi_authenticated"] = True
            st.rerun()

        else:
            st.error("Invalid username or password.")

    st.stop()


with st.sidebar:

    if st.button("Log out"):
        st.session_state["npi_authenticated"] = False
        st.rerun()


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

    module = st.radio(
        "Select module",
        [
            "Control Tower",
            "Data Validation",
            "Unique-Part Validation",
            "Calculation Layer",
            "Management Commitment",
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
                                "28px";

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

    cx = 135
    cy = 135

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
                st.session_state["selected_classification"] = "ALL"
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
            st.session_state["selected_classification"] = "ALL"
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
    # 7. FLOATING HTML
    # ============================================================

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
    height: 455px;

    margin: 0;
    padding: 0;

    background: transparent;

    font-family:
        Arial,
        Helvetica,
        sans-serif;
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
   ENQUIRY FINDER
   ============================================================ */

.npi-enquiry-floating-content {{
    width: 100%;

    height:
        calc(100% - 38px);

    display: flex;

    align-items: center;

    justify-content: center;

    padding: 12px;

    color: #777777;

    font-size: 12px;

    text-align: center;
}}

</style>


<div
    class="npi-floating-stage"
    id="npi-floating-stage"
>

    <!-- ========================================================
         ENQUIRY FINDER
         ======================================================== -->

    <div
        class="npi-floating-panel"
        id="npi-enquiry-panel"
        data-layout-id="enquiry"
        style="
            left:12px;
            top:12px;
            width:270px;
            height:170px;
        "
    >

        <div
            class="npi-floating-header"
            data-drag-handle
        >

            <span>
                ENQUIRY FINDER
            </span>

        </div>


        <div
            class="npi-enquiry-floating-content"
        >

            ENQUIRY FINDER<br>
            RESERVED SPACE

        </div>

    </div>


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

            <span>
                EXECUTIVE STATUS
            </span>


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

</div>


<script>

(function () {{

    /* ========================================================
       STORAGE
       ======================================================== */

    const STORAGE_KEY =
        "npi_control_tower_top01_layout_v2";


    const DEFAULT_LAYOUT = {{

        enquiry: {{
            left: 12,
            top: 12,
            width: 270,
            height: 170
        }},

        executive: {{
            left: 300,
            top: 12,
            width: 650,
            height: 370
        }},

        delayBucket: {{
            left: 12,
            top: 200,
            width: 270,
            height: 245
        }},

        hotspot: {{
            left: 970,
            top: 12,
            width: 300,
            height: 300
        }}

    }};


    /* ========================================================
       GENERAL HELPERS
       ======================================================== */

    function getParentDocument() {{

        try {{

            return window.parent.document;

        }}
        catch (error) {{

            return null;

        }}

    }}


    function clamp(
        value,
        minimum,
        maximum
    ) {{

        return Math.max(
            minimum,
            Math.min(
                maximum,
                value
            )
        );

    }}


    /* ========================================================
       LAYOUT SAVE
       ======================================================== */

    function saveLayout() {{

        try {{

            const layout = {{}};

            document
                .querySelectorAll(
                    ".npi-floating-panel"
                )
                .forEach(
                    function(panel) {{

                        const id =
                            panel.dataset.layoutId;

                        layout[id] = {{

                            left:
                                panel.offsetLeft,

                            top:
                                panel.offsetTop,

                            width:
                                panel.offsetWidth,

                            height:
                                panel.offsetHeight

                        }};

                    }}
                );


            try {{

                localStorage.setItem(
                    STORAGE_KEY,
                    JSON.stringify(layout)
                );

                return;

            }}
            catch (error) {{}}


            try {{

                window.parent.localStorage.setItem(
                    STORAGE_KEY,
                    JSON.stringify(layout)
                );

            }}
            catch (error) {{}}

        }}
        catch (error) {{}}

    }}


    /* ========================================================
       LAYOUT LOAD
       ======================================================== */

    function loadLayout() {{

        try {{

            let raw = null;


            try {{

                raw =
                    localStorage.getItem(
                        STORAGE_KEY
                    );

            }}
            catch (error) {{}}


            if (!raw) {{

                try {{

                    raw =
                        window.parent.localStorage.getItem(
                            STORAGE_KEY
                        );

                }}
                catch (error) {{}}

            }}


            if (!raw) {{
                return;
            }}


            const layout =
                JSON.parse(raw);


            Object.keys(layout)
                .forEach(
                    function(id) {{

                        const panel =
                            document.querySelector(
                                '[data-layout-id="' +
                                id +
                                '"]'
                            );


                        if (!panel) {{
                            return;
                        }}


                        const item =
                            layout[id];


                        if (
                            Number.isFinite(
                                item.left
                            )
                        ) {{

                            panel.style.left =
                                item.left + "px";

                        }}


                        if (
                            Number.isFinite(
                                item.top
                            )
                        ) {{

                            panel.style.top =
                                item.top + "px";

                        }}


                        if (
                            Number.isFinite(
                                item.width
                            )
                        ) {{

                            panel.style.width =
                                item.width + "px";

                        }}


                        if (
                            Number.isFinite(
                                item.height
                            )
                        ) {{

                            panel.style.height =
                                item.height + "px";

                        }}

                    }}
                );

        }}
        catch (error) {{}}

    }}


    /* ========================================================
       RESET
       ======================================================== */

    function resetLayout() {{

        try {{

            localStorage.removeItem(
                STORAGE_KEY
            );

        }}
        catch (error) {{}}


        try {{

            window.parent.localStorage.removeItem(
                STORAGE_KEY
            );

        }}
        catch (error) {{}}


        Object.keys(DEFAULT_LAYOUT)
            .forEach(
                function(id) {{

                    const panel =
                        document.querySelector(
                            '[data-layout-id="' +
                            id +
                            '"]'
                        );


                    if (!panel) {{
                        return;
                    }}


                    const item =
                        DEFAULT_LAYOUT[id];


                    panel.style.left =
                        item.left + "px";

                    panel.style.top =
                        item.top + "px";

                    panel.style.width =
                        item.width + "px";

                    panel.style.height =
                        item.height + "px";

                }}
            );


        saveLayout();

    }}


    /* ========================================================
       DRAGGING
       ======================================================== */

    function enableDragging(panel) {{

        const handle =
            panel.querySelector(
                "[data-drag-handle]"
            );


        if (!handle) {{
            return;
        }}


        let dragging = false;

        let startX = 0;
        let startY = 0;

        let startLeft = 0;
        let startTop = 0;


        handle.addEventListener(
            "pointerdown",
            function(event) {{

                if (
                    event.target.closest(
                        "button"
                    )
                ) {{
                    return;
                }}


                dragging = true;


                startX =
                    event.clientX;

                startY =
                    event.clientY;


                startLeft =
                    panel.offsetLeft;

                startTop =
                    panel.offsetTop;


                try {{

                    handle.setPointerCapture(
                        event.pointerId
                    );

                }}
                catch (error) {{}}


                event.preventDefault();

            }}
        );


        handle.addEventListener(
            "pointermove",
            function(event) {{

                if (!dragging) {{
                    return;
                }}


                const stage =
                    document.getElementById(
                        "npi-floating-stage"
                    );


                if (!stage) {{
                    return;
                }}


                const dx =
                    event.clientX
                    - startX;


                const dy =
                    event.clientY
                    - startY;


                const maxLeft =
                    Math.max(
                        0,
                        stage.clientWidth
                        - panel.offsetWidth
                    );


                const maxTop =
                    Math.max(
                        0,
                        stage.clientHeight
                        - panel.offsetHeight
                    );


                panel.style.left =
                    clamp(
                        startLeft + dx,
                        0,
                        maxLeft
                    ) + "px";


                panel.style.top =
                    clamp(
                        startTop + dy,
                        0,
                        maxTop
                    ) + "px";

            }}
        );


        handle.addEventListener(
            "pointerup",
            function() {{

                dragging = false;

                saveLayout();

            }}
        );


        handle.addEventListener(
            "pointercancel",
            function() {{

                dragging = false;

                saveLayout();

            }}
        );

    }}


    /* ========================================================
       RESIZE SAVE
       ======================================================== */

    function enableResizeSaving(panel) {{

        if (
            typeof ResizeObserver
            ===
            "undefined"
        ) {{
            return;
        }}


        let lastWidth =
            panel.offsetWidth;


        let lastHeight =
            panel.offsetHeight;


        const observer =
            new ResizeObserver(
                function() {{

                    if (
                        panel.offsetWidth
                        !==
                        lastWidth
                        ||
                        panel.offsetHeight
                        !==
                        lastHeight
                    ) {{

                        lastWidth =
                            panel.offsetWidth;

                        lastHeight =
                            panel.offsetHeight;


                        saveLayout();

                    }}

                }}
            );


        observer.observe(panel);

    }}


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
                ">2 YEARS"
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

        wireLegend();

        wireDonutCenter();

        wireDelayBucketTiles();

        wireHotspotRows();

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

    components.html(
        floating_html,
        height=470,
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
        Owner
        Action
        Support Required
        Commitment Date
        Action Status
        Actual Completion
        Remarks

    Layout:
    - First 3 columns are horizontally frozen.
    - Horizontal scroll begins after PICTURE.
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
    # SORT
    # ========================================================

    display_df = df.copy()

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
    # COMPACT SINGLE-LINE DRILLDOWN HEADER + PAGINATION
    # ========================================================

    # Store the current page independently of Streamlit widget state so that
    # changing a legend tile / Back to Top does not leave stale page controls.
    page_state_key = f"drilldown_page_state_{title}"
    page = int(st.session_state.get(page_state_key, 1))
    page = max(1, min(page, total_pages))
    st.session_state[page_state_key] = page

    # Responsive single-row Drilldown header. The control date is intentionally
    # omitted here; the Excel export sits in the same row as the title/controls.
    st.markdown(
        """
        <style>
        div[class*="st-key-drilldown_compact_header"] {
            width: 100% !important;
            max-width: 100% !important;
        }
        div[class*="st-key-drilldown_compact_header"] > div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-wrap: nowrap !important;
            align-items: center !important;
            gap: 0.55rem !important;
            width: 100% !important;
        }
        div[class*="st-key-drilldown_compact_header"] > div[data-testid="stHorizontalBlock"] > [data-testid="column"] {
            flex: 1 1 0 !important;
            width: 0 !important;
            min-width: 0 !important;
            max-width: none !important;
            padding: 0 !important;
            margin: 0 !important;
        }
        div[class*="st-key-drilldown_compact_header"] [data-testid="stButton"] button {
            min-height: 1.8rem !important;
            height: 1.8rem !important;
            padding: 0 0.4rem !important;
            white-space: nowrap !important;
        }
        div[class*="st-key-drilldown_compact_header"] [data-testid="stHorizontalBlock"] [data-testid="stHorizontalBlock"] {
            gap: 0.2rem !important;
            width: 100% !important;
        }
        div[class*="st-key-drilldown_compact_header"] [data-testid="stHorizontalBlock"] [data-testid="stHorizontalBlock"] > [data-testid="column"] {
            flex: 1 1 0 !important;
            width: 0 !important;
            min-width: 0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    meta_style = "font-size:14px;color:#666;white-space:nowrap;line-height:1.3;"

    with st.container(key="drilldown_compact_header"):
        header_cols = st.columns(
            [3.0, 1.8, 3.1, 2.5, 2.5],
            vertical_alignment="center",
            gap="small",
        )

        header_cols[0].markdown(
            f"<div style='font-size:18px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>"
            f"{html_escape(title)}</div>",
            unsafe_allow_html=True,
        )
        header_cols[1].markdown(
            f"<div style='{meta_style}overflow:hidden;text-overflow:ellipsis;'>CUSTOMER: "
            f"{html_escape(str(customer_display))}</div>",
            unsafe_allow_html=True,
        )

        with header_cols[2]:
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
                f"<div style='{meta_style}text-align:center;'>Page {page}</div>",
                unsafe_allow_html=True,
            )
            page_controls[3].button(
                "+",
                key=f"drilldown_next_page_{title}",
                disabled=(page >= total_pages),
                on_click=lambda k=page_state_key, p=page, n=total_pages: st.session_state.__setitem__(k, min(n, p + 1)),
            )
            page_controls[4].markdown(
                f"<div style='{meta_style}'>of {total_pages}</div>",
                unsafe_allow_html=True,
            )

        header_cols[3].markdown(
            f"<div style='{meta_style}overflow:hidden;text-overflow:ellipsis;'>Showing "
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
    # ACTION LOOKUP
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
            "key": "picture",
            "heading": "PICTURE",
            "source": None,
            "type": "picture",
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
            "heading": "Owner",
        },
        {
            "key": "action",
            "heading": "Action",
        },
        {
            "key": "support_required",
            "heading": "Support Required",
        },
        {
            "key": "commitment_date",
            "heading": "Commitment Date",
        },
        {
            "key": "action_status",
            "heading": "Action Status",
        },
        {
            "key": "actual_completion",
            "heading": "Actual Completion",
        },
        {
            "key": "remarks",
            "heading": "Remarks",
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
        "PICTURE": 95,
        "PO NUMBER": 115,
        "ENGINEER": 105,
        "JOB NO": 100,
        "CURRENT PROCESS": 135,
        "NEXT PROCESS": 125,
        "DELAY DAYS": 95,
        "ORDER DATE": 105,
        "AGING": 80,
        "PRIORITY": 80,

        "Owner": 100,
        "Action": 180,
        "Support Required": 180,
        "Commitment Date": 125,
        "Action Status": 120,
        "Actual Completion": 130,
        "Remarks": 180,
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

    sticky_left = {
        "CUS": 0,
        "PART NO": cus_width,
        "PICTURE": (
            cus_width
            + part_width
        ),
    }

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

        # Two-level header: fixed columns span both header rows; COMMITMENT spans seven subcolumns.
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
        header_cols[4].download_button(
            "Download Drilldown Excel",
            data=excel_buffer.getvalue(),
            file_name="NPI_Control_Tower_Drilldown.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_current_drilldown_excel",
            use_container_width=True,
        )
    except Exception as exc:
        header_cols[4].warning(f"Excel export is unavailable: {exc}")

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
        f"""
        <div style="
            width:100%;
            overflow-x:auto;
            overflow-y:auto;
            max-height:calc(100vh - 220px);
            min-height:553px;
            border:1px solid #d0d0d0;
            position:relative;
            background:#ffffff;
        ">
        """
    )

    html_parts.append(
        """
        <table style="
            border-collapse:separate;
            border-spacing:0;
            table-layout:fixed;
            width:max-content;
            min-width:100%;
            font-family:Arial,sans-serif;
            font-size:12px;
        ">
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
            <th
                rowspan="2"
                style="
                    width:{width}px;
                    min-width:{width}px;
                    max-width:{width}px;
                    height:{TOTAL_HEADER_HEIGHT}px;
                    padding:5px 5px;
                    border-right:1px solid #bdbdbd;
                    border-bottom:1px solid #bdbdbd;
                    border-left:1px solid #bdbdbd;
                    text-align:center;
                    vertical-align:middle;
                    font-weight:700;
                    line-height:1.15;
                    white-space:normal;
                    overflow-wrap:break-word;
                    box-sizing:border-box;

                    position:sticky !important;
                    top:0px !important;

                    background:#f2f2f2 !important;

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
            colspan="7"
            style="
                width:{commitment_total_width}px;
                min-width:{commitment_total_width}px;
                height:{MAIN_HEADER_HEIGHT}px;
                padding:5px 5px;
                border-right:1px solid #bdbdbd;
                border-bottom:1px solid #bdbdbd;
                border-left:1px solid #bdbdbd;
                text-align:center;
                vertical-align:middle;
                font-weight:700;
                line-height:1.15;
                white-space:normal;
                box-sizing:border-box;

                position:sticky !important;
                top:0px !important;

                z-index:45;
                background:#f2f2f2 !important;
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
                    border-right:1px solid #bdbdbd;
                    border-bottom:1px solid #bdbdbd;
                    border-left:1px solid #bdbdbd;
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
                    background:#f2f2f2 !important;
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

    for _, row in page_df.iterrows():

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

        html_parts.append(
            f"""
            <tr style="
                height:{BODY_ROW_HEIGHT}px;
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
                                        max-width:80px;
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
                    background:#ffffff;
                """

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
                        border-right:1px solid #d0d0d0;
                        border-bottom:1px solid #d0d0d0;
                        border-left:1px solid #d0d0d0;
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

            action_value = ""

            if key == "owner":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "OWNER",
                            "Owner",
                        ],
                    )
                )

            elif key == "action":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "ACTION",
                            "Action",
                        ],
                    )
                )

            elif key == "support_required":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "SUPPORT_REQUIRED",
                            "Support Required",
                            "SUPPORT",
                            "Support",
                        ],
                    )
                )

            elif key == "commitment_date":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "COMMITMENT_DATE",
                            "Commitment Date",
                        ],
                    )
                )

                if action_value:

                    action_value = (
                        fmt_date(
                            action_value
                        )
                    )

            elif key == "action_status":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "ACTION_STATUS",
                            "Action Status",
                            "STATUS",
                            "Commitment Status",
                        ],
                    )
                )

            elif key == "actual_completion":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "ACTUAL_COMPLETION",
                            "Actual Completion",
                            "COMPLETION_DATE",
                            "Completion Date",
                        ],
                    )
                )

                if action_value:

                    action_value = (
                        fmt_date(
                            action_value
                        )
                    )

            elif key == "remarks":

                action_value = (
                    get_action_value(
                        action,
                        [
                            "REMARKS",
                            "Remarks",
                            "NOTE",
                            "Notes",
                        ],
                    )
                )

            cell_content = html_escape(
                safe_text(
                    action_value
                )
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
                        border-right:1px solid #d0d0d0;
                        border-bottom:1px solid #d0d0d0;
                        border-left:1px solid #d0d0d0;
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

    components.html(
        "".join(html_parts),
        height=760,
        scrolling=False,
    )


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

    # Keep the customer label immediately beside its selector.
    # Keep the customer label/selector immediately after the title and date.
    # The title column is deliberately narrower; its single-line content may
    # overflow visually, while the next columns begin close to the text end.
    header_title_col, header_customer_label_col, header_customer_col, _header_spacer_col = st.columns(
        [3.8, 0.72, 1.45, 4.03],
        vertical_alignment="center",
        gap="small",
    )

    with header_title_col:
        st.markdown(
            f"""
            <div style="display:flex; align-items:center; gap:12px; white-space:nowrap; overflow:visible;">
                <span class="npi-control-tower-top-title">NPI CONTROL TOWER</span>
                <span class="npi-control-tower-top-meta">| Control Date: {control_date.strftime('%d/%m/%Y')} |</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with header_customer_label_col:
        st.markdown(
            '<div class="npi-control-tower-top-meta" style="white-space:nowrap;">Customer:</div>',
            unsafe_allow_html=True,
        )

    with header_customer_col:
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
        # A hotspot selection belongs to the previous customer's context.
        # Clear it so the new customer starts with ALL unique parts.
        st.session_state["selected_hotspot"] = ""

        # Discard any stale donut URL event before rerunning.
        if "npi_exec" in st.query_params:
            del st.query_params["npi_exec"]

        # Start the new customer's Drilldown at page 1, but do not trigger
        # the global auto-scroll controller. Keep the user's current viewport.
        for page_key in list(st.session_state.keys()):
            if str(page_key).startswith("drilldown_page_state_"):
                st.session_state[page_key] = 1

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

    st.divider()

    # --------------------------------------------------------
    # MANAGEMENT ACTION SUMMARY
    # --------------------------------------------------------

    st.markdown(
        "### MANAGEMENT ACTION"
    )

    actions_df = (
        st.session_state.actions_df
    )

    open_action = 0
    overdue = 0
    due_today = 0
    upcoming = 0
    no_commitment = 0

    if (
        actions_df is not None
        and not actions_df.empty
    ):

        for _, action_row in (
            actions_df.iterrows()
        ):

            status = (
                calculate_commitment_status(
                    action_row.get(
                        "COMMITMENT_DATE"
                    ),
                    action_row.get(
                        "ACTION_STATUS"
                    ),
                    control_date,
                )
            )

            if status != "COMPLETED":
                open_action += 1

            if status == "OVERDUE":
                overdue += 1

            elif status == "DUE TODAY":
                due_today += 1

            elif status == "UPCOMING":
                upcoming += 1

            elif status == "NO COMMITMENT":
                no_commitment += 1

    action_cols = st.columns(5)

    action_cols[0].metric(
        "OPEN ACTION",
        open_action,
    )

    action_cols[1].metric(
        "OVERDUE",
        overdue,
    )

    action_cols[2].metric(
        "DUE TODAY",
        due_today,
    )

    action_cols[3].metric(
        "UPCOMING",
        upcoming,
    )

    action_cols[4].metric(
        "NO COMMITMENT",
        no_commitment,
    )

    st.divider()

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
        drill_title = f"PRODUCTION HOTSPOT: {selected_hotspot} ({len(drill_df):,})"
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
# MANAGEMENT COMMITMENT
# ============================================================

def render_management_commitment():

    render_title(
        "MANAGEMENT COMMITMENT",
        (
            "Management action and "
            "commitment queue."
        ),
    )

    actions_df = (
        st.session_state.actions_df
    )

    open_action = 0
    overdue = 0
    due_today = 0
    upcoming = 0
    no_commitment = 0

    if (
        actions_df is not None
        and not actions_df.empty
    ):

        for _, action_row in (
            actions_df.iterrows()
        ):

            status = (
                calculate_commitment_status(
                    action_row.get(
                        "COMMITMENT_DATE"
                    ),
                    action_row.get(
                        "ACTION_STATUS"
                    ),
                    control_date,
                )
            )

            if status != "COMPLETED":
                open_action += 1

            if status == "OVERDUE":
                overdue += 1

            elif status == "DUE TODAY":
                due_today += 1

            elif status == "UPCOMING":
                upcoming += 1

            elif status == "NO COMMITMENT":
                no_commitment += 1

    cols = st.columns(5)

    cols[0].metric(
        "OPEN ACTION",
        open_action,
    )

    cols[1].metric(
        "OVERDUE",
        overdue,
    )

    cols[2].metric(
        "DUE TODAY",
        due_today,
    )

    cols[3].metric(
        "UPCOMING",
        upcoming,
    )

    cols[4].metric(
        "NO COMMITMENT",
        no_commitment,
    )

    st.divider()

    st.markdown(
        "### COMMITMENT QUEUE"
    )

    if (
        actions_df is None
        or actions_df.empty
    ):

        st.info(
            "No management commitments "
            "recorded yet."
        )

    else:

        st.dataframe(
            actions_df,
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

elif module == "Management Commitment":

    render_management_commitment()
