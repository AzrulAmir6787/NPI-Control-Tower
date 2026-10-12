import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

PART_COLUMN = "Subpart Part Num"
ORDER_DATE_COLUMN = "Order Date"
JOB_COLUMN = "JobNum/Asm"

NO_JOB_VALUE = "NO JOB"


# ============================================================
# STEP DEFINITIONS
# ============================================================

STEP_COLUMNS = [
    f"Step {i}"
    for i in range(1, 21)
]

PLAN_COLUMNS = [
    f"Job Oper {i} Plan Start Date"
    for i in range(10, 201, 10)
]

ACTUAL_COLUMNS = [
    f"Job Oper {i} Actual Start Date"
    for i in range(10, 201, 10)
]


# ============================================================
# BASIC HELPERS
# ============================================================

def _clean_text(value):
    """
    Convert value to clean uppercase text.
    """
    if pd.isna(value):
        return ""

    return str(value).strip()


def _normalize_date(value):
    """
    Convert value to pandas Timestamp.
    Invalid values become NaT.
    """
    return pd.to_datetime(
        value,
        errors="coerce",
    )


# ============================================================
# CUSTOMER EXTRACTION
# ============================================================

def extract_customer(part_number):
    """
    Customer is the text before the FIRST dash in Column E.

    Example:
        ABB-12345 -> ABB
        ASM-1330-060-01 -> ASM
        HP-HPM-3FW27-60020-01 -> HP

    The complete Column E value remains the authoritative
    part identity.
    """

    if pd.isna(part_number):
        return ""

    value = str(part_number).strip()

    if not value:
        return ""

    if "-" in value:
        return value.split("-", 1)[0].strip()

    return value


# ============================================================
# PRIORITY
# ============================================================

def calculate_priority(delay_days):
    """
    Priority rules:

        > 90 days  = P1
        31-90      = P2
        8-30       = P3
        <= 7       = P4
    """

    if pd.isna(delay_days):
        return ""

    try:
        delay = float(delay_days)
    except Exception:
        return ""

    if delay > 90:
        return "P1"

    if delay >= 31:
        return "P2"

    if delay >= 8:
        return "P3"

    return "P4"


# ============================================================
# ORDER AGE
# ============================================================

def calculate_order_age(
    order_date,
    control_date,
):
    """
    Order Age:

        Control Date - Order Date

    Used for:
        Engineering
        Process
    """

    order = _normalize_date(order_date)
    control = _normalize_date(control_date)

    if pd.isna(order) or pd.isna(control):
        return pd.NA

    return max(
        0,
        (control.normalize() - order.normalize()).days,
    )


# ============================================================
# PRODUCTION DELAY AGE
# ============================================================

def calculate_production_delay_age(
    delay_days,
):
    """
    Production Aging is based directly on Delay Days.

    This is intentionally NOT Order Age.
    """

    if pd.isna(delay_days):
        return pd.NA

    try:
        delay = float(delay_days)
    except Exception:
        return pd.NA

    return max(0, int(delay))


# ============================================================
# CURRENT PROCESS / ROUTING
# ============================================================

def determine_current_operation(
    row,
    scan_gap=True,
):
    """
    Determine current operation.

    Priority:
        1. Engineering pending
        2. Routing not ready
        3. First process if no actual exists
        4. Scan-gap process after latest actual
        5. Complete

    If all defined processes are complete:
        COMPLETE
    """

    job_value = _clean_text(row.get(JOB_COLUMN))

    # --------------------------------------------------------
    # Engineering
    # --------------------------------------------------------

    if job_value.upper() == NO_JOB_VALUE:
        return "ENGINEERING"

    # --------------------------------------------------------
    # Defined processes
    # --------------------------------------------------------

    defined_steps = []

    for index, step_column in enumerate(STEP_COLUMNS):

        if step_column not in row.index:
            continue

        process = _clean_text(row.get(step_column))

        if process:
            defined_steps.append(
                {
                    "index": index,
                    "process": process,
                }
            )

    if not defined_steps:
        return "ROUTING NOT READY"

    # --------------------------------------------------------
    # Actual status
    # --------------------------------------------------------

    actual_status = []

    for index, step_column in enumerate(STEP_COLUMNS):

        if index >= len(ACTUAL_COLUMNS):
            break

        actual_column = ACTUAL_COLUMNS[index]

        actual_value = (
            row.get(actual_column)
            if actual_column in row.index
            else None
        )

        actual_status.append(
            {
                "index": index,
                "actual": not pd.isna(actual_value)
                and str(actual_value).strip() != "",
            }
        )

    # --------------------------------------------------------
    # No actual process started
    # --------------------------------------------------------

    completed_indices = {
        item["index"]
        for item in actual_status
        if item["actual"]
    }

    if not completed_indices:

        return defined_steps[0]["process"]

    # --------------------------------------------------------
    # Scan gap
    # --------------------------------------------------------

    if scan_gap:

        latest_actual = max(completed_indices)

        for step in defined_steps:

            if step["index"] > latest_actual:
                return step["process"]

    # --------------------------------------------------------
    # First incomplete defined process
    # --------------------------------------------------------

    for step in defined_steps:

        if step["index"] not in completed_indices:
            return step["process"]

    # --------------------------------------------------------
    # All defined processes complete
    # --------------------------------------------------------

    return "COMPLETE"


# ============================================================
# NEXT OPERATION
# ============================================================

def determine_next_operation(
    row,
    current_operation,
    scan_gap=True,
):
    """
    Determine next operation after current operation.
    """

    if current_operation in [
        "ENGINEERING",
        "ROUTING NOT READY",
        "COMPLETE",
    ]:
        return ""

    defined_steps = []

    for index, step_column in enumerate(STEP_COLUMNS):

        if step_column not in row.index:
            continue

        process = _clean_text(row.get(step_column))

        if process:
            defined_steps.append(
                {
                    "index": index,
                    "process": process,
                }
            )

    if not defined_steps:
        return ""

    # --------------------------------------------------------
    # Find current operation
    # --------------------------------------------------------

    current_index = None

    for step in defined_steps:

        if step["process"] == current_operation:
            current_index = step["index"]
            break

    if current_index is None:
        return ""

    # --------------------------------------------------------
    # Find next defined process
    # --------------------------------------------------------

    for step in defined_steps:

        if step["index"] > current_index:
            return step["process"]

    return ""


# ============================================================
# MISSED SCAN DIAGNOSTICS
# ============================================================

def calculate_scan_gap_details(
    row,
):
    """
    Diagnostic information for scan gaps.
    """

    defined_steps = []

    for index, step_column in enumerate(STEP_COLUMNS):

        if step_column not in row.index:
            continue

        process = _clean_text(row.get(step_column))

        if process:
            defined_steps.append(
                {
                    "index": index,
                    "process": process,
                }
            )

    completed_indices = set()

    for index, actual_column in enumerate(ACTUAL_COLUMNS):

        if actual_column not in row.index:
            continue

        actual_value = row.get(actual_column)

        if (
            not pd.isna(actual_value)
            and str(actual_value).strip() != ""
        ):
            completed_indices.add(index)

    if not completed_indices:

        return {
            "last_actual_process": "",
            "scan_gap_process": "",
            "missed_scan_count": 0,
        }

    latest_actual_index = max(completed_indices)

    latest_actual_process = ""

    for step in defined_steps:

        if step["index"] == latest_actual_index:
            latest_actual_process = step["process"]
            break

    missed_processes = [
        step["process"]
        for step in defined_steps
        if (
            step["index"] < latest_actual_index
            and step["index"] not in completed_indices
        )
    ]

    scan_gap_process = ""

    for step in defined_steps:

        if step["index"] > latest_actual_index:
            scan_gap_process = step["process"]
            break

    return {
        "last_actual_process": latest_actual_process,
        "scan_gap_process": scan_gap_process,
        "missed_scan_count": len(missed_processes),
    }


# ============================================================
# CALCULATE DELAY
# ============================================================

def calculate_delay(
    row,
    control_date,
):
    """
    Delay Days:

        Control Date - current process plan date

    Current process plan date is matched to the first
    defined process in the routing sequence.

    If no matching plan date exists, returns NaN.
    """

    current_operation = row.get(
        "CALC_CURRENT_OPERATION",
        "",
    )

    if not current_operation:
        return pd.NA

    if current_operation in [
        "ENGINEERING",
        "ROUTING NOT READY",
        "COMPLETE",
    ]:
        return pd.NA

    # --------------------------------------------------------
    # Find process index
    # --------------------------------------------------------

    process_index = None

    for index, step_column in enumerate(STEP_COLUMNS):

        if step_column not in row.index:
            continue

        process = _clean_text(row.get(step_column))

        if process == current_operation:
            process_index = index
            break

    if process_index is None:
        return pd.NA

    # --------------------------------------------------------
    # Match process index to plan date
    # --------------------------------------------------------

    if process_index >= len(PLAN_COLUMNS):
        return pd.NA

    plan_column = PLAN_COLUMNS[process_index]

    if plan_column not in row.index:
        return pd.NA

    plan_date = _normalize_date(
        row.get(plan_column)
    )

    control = _normalize_date(control_date)

    if pd.isna(plan_date) or pd.isna(control):
        return pd.NA

    return (
        control.normalize()
        - plan_date.normalize()
    ).days


# ============================================================
# CLASSIFICATION
# ============================================================

def calculate_classification(row):
    """
    NPI classification hierarchy:

        1. RED - ENGINEERING
        2. RED - PROCESS
        3. GREEN - COMPLETE
        4. RED - PRODUCTION
        5. GREEN - ON TRACK
    """

    current_operation = _clean_text(
        row.get("CALC_CURRENT_OPERATION")
    )

    job_value = _clean_text(
        row.get(JOB_COLUMN)
    )

    # --------------------------------------------------------
    # Engineering
    # --------------------------------------------------------

    if job_value.upper() == NO_JOB_VALUE:
        return "RED - ENGINEERING"

    # --------------------------------------------------------
    # Process / routing
    # --------------------------------------------------------

    if current_operation == "ROUTING NOT READY":
        return "RED - PROCESS"

    # --------------------------------------------------------
    # Complete
    # --------------------------------------------------------

    if current_operation == "COMPLETE":
        return "GREEN - COMPLETE"

    # --------------------------------------------------------
    # Production
    # --------------------------------------------------------

    delay = row.get("CALC_DELAY_DAYS")

    if not pd.isna(delay):

        try:
            if float(delay) > 0:
                return "RED - PRODUCTION"

        except Exception:
            pass

    # --------------------------------------------------------
    # On track
    # --------------------------------------------------------

    return "GREEN - ON TRACK"


# ============================================================
# BUILD CALCULATION LAYER
# ============================================================

def build_calculation_layer(
    df,
    control_date,
    scan_gap=True,
    scan_gap_mode=None,
):
    """
    Build the complete calculation layer from Virgin BAQ.

    No historical WPS/VBA values are used.
    """

    if scan_gap_mode is not None:
        scan_gap = scan_gap_mode

    data = df.copy()
    # --------------------------------------------------------
    # Validate required columns
    # --------------------------------------------------------

    required_columns = [
        PART_COLUMN,
        ORDER_DATE_COLUMN,
        JOB_COLUMN,
    ]

    missing = [
        column
        for column in required_columns
        if column not in data.columns
    ]

    if missing:
        raise ValueError(
            "Required columns missing from BAQ: "
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # Normalize key dates
    # --------------------------------------------------------

    data[ORDER_DATE_COLUMN] = pd.to_datetime(
        data[ORDER_DATE_COLUMN],
        errors="coerce",
    )

    control = _normalize_date(control_date)

    # --------------------------------------------------------
    # Keep rows with valid Order Date
    # --------------------------------------------------------

    data = data[
        data[ORDER_DATE_COLUMN].notna()
    ].copy()

    # --------------------------------------------------------
    # Customer
    # --------------------------------------------------------

    data["CALC_CUSTOMER"] = data[
        PART_COLUMN
    ].apply(extract_customer)

    # --------------------------------------------------------
    # Order Age
    # --------------------------------------------------------

    data["CALC_ORDER_AGE_DAYS"] = data[
        ORDER_DATE_COLUMN
    ].apply(
        lambda value: calculate_order_age(
            value,
            control,
        )
    )

    # --------------------------------------------------------
    # Current Operation
    # --------------------------------------------------------

    data["CALC_CURRENT_OPERATION"] = data.apply(
        lambda row: determine_current_operation(
            row,
            scan_gap=scan_gap,
        ),
        axis=1,
    )

    # --------------------------------------------------------
    # Next Operation
    # --------------------------------------------------------

    data["CALC_NEXT_OPERATION"] = data.apply(
        lambda row: determine_next_operation(
            row,
            row.get("CALC_CURRENT_OPERATION", ""),
            scan_gap=scan_gap,
        ),
        axis=1,
    )

    # --------------------------------------------------------
    # Delay Days
    # --------------------------------------------------------

    data["CALC_DELAY_DAYS"] = data.apply(
        lambda row: calculate_delay(
            row,
            control,
        ),
        axis=1,
    )

    # --------------------------------------------------------
    # Production Delay Age
    # --------------------------------------------------------

    data["CALC_PRODUCTION_DELAY_AGE"] = data[
        "CALC_DELAY_DAYS"
    ].apply(
        calculate_production_delay_age
    )

    # --------------------------------------------------------
    # Priority
    # --------------------------------------------------------

    data["CALC_PRIORITY"] = data[
        "CALC_DELAY_DAYS"
    ].apply(
        calculate_priority
    )

    # --------------------------------------------------------
    # Scan-gap diagnostics
    # --------------------------------------------------------

    scan_details = data.apply(
        calculate_scan_gap_details,
        axis=1,
    )

    data["CALC_LAST_ACTUAL_PROCESS"] = (
        scan_details.apply(
            lambda x: x["last_actual_process"]
        )
    )

    data["CALC_SCAN_GAP_PROCESS"] = (
        scan_details.apply(
            lambda x: x["scan_gap_process"]
        )
    )

    data["CALC_MISSED_SCAN_COUNT"] = (
        scan_details.apply(
            lambda x: x["missed_scan_count"]
        )
    )

    # --------------------------------------------------------
    # Classification
    # --------------------------------------------------------

    data["CALC_CLASSIFICATION"] = data.apply(
        calculate_classification,
        axis=1,
    )

    return data.reset_index(drop=True)


# ============================================================
# UNIQUE PART RESOLUTION
# ============================================================

def resolve_unique_parts(df):
    """
    Resolve duplicate Full Column E parts.

    LOCKED RULE:

        Calculate delay for every occurrence.
        Keep the occurrence with the LOWEST numeric delay.

    If all duplicate occurrences have NaN delay,
    stable original order is retained.
    """

    data = df.copy()

    if PART_COLUMN not in data.columns:
        raise ValueError(
            f"Required part column '{PART_COLUMN}' not found."
        )

    # --------------------------------------------------------
    # Preserve original order
    # --------------------------------------------------------

    data["_CALC_ORIGINAL_ORDER"] = range(len(data))

    # --------------------------------------------------------
    # Clean full part key
    # --------------------------------------------------------

    data["_CALC_PART_KEY"] = (
        data[PART_COLUMN]
        .astype("string")
        .str.strip()
    )

    # --------------------------------------------------------
    # Numeric delay
    # --------------------------------------------------------

    data["_CALC_DELAY_NUMERIC"] = pd.to_numeric(
        data.get("CALC_DELAY_DAYS"),
        errors="coerce",
    )

    # --------------------------------------------------------
    # Sort:
    #
    # 1. Full part number
    # 2. Lowest delay
    # 3. Original order
    # 4. NaN delay last
    # --------------------------------------------------------

    data["_CALC_DELAY_IS_NAN"] = (
        data["_CALC_DELAY_NUMERIC"].isna()
    )

    data = data.sort_values(
        by=[
            "_CALC_PART_KEY",
            "_CALC_DELAY_IS_NAN",
            "_CALC_DELAY_NUMERIC",
            "_CALC_ORIGINAL_ORDER",
        ],
        ascending=[
            True,
            True,
            True,
            True,
        ],
        kind="mergesort",
    )

    # --------------------------------------------------------
    # Keep one occurrence per full Column E
    # --------------------------------------------------------

    data = data.drop_duplicates(
        subset=["_CALC_PART_KEY"],
        keep="first",
    )

    # --------------------------------------------------------
    # Restore original order of retained records
    # --------------------------------------------------------

    data = data.sort_values(
        "_CALC_ORIGINAL_ORDER",
        kind="mergesort",
    )

    # --------------------------------------------------------
    # Remove helper columns
    # --------------------------------------------------------

    data = data.drop(
        columns=[
            "_CALC_ORIGINAL_ORDER",
            "_CALC_PART_KEY",
            "_CALC_DELAY_NUMERIC",
            "_CALC_DELAY_IS_NAN",
        ],
        errors="ignore",
    )

    return data.reset_index(drop=True)


# ============================================================
# BUILD UNIQUE PART DATASET
# ============================================================

def build_unique_part_dataset(
    calculation_df,
):
    """
    Resolve calculation records into one representative
    occurrence per Full Column E part.
    """

    return resolve_unique_parts(
        calculation_df
    )


# ============================================================
# UNIQUE PART COUNTS
# ============================================================

def calculate_unique_parts(df):
    """
    Count distinct Full Column E parts.
    """

    if PART_COLUMN not in df.columns:
        return 0

    return (
        df[PART_COLUMN]
        .dropna()
        .astype(str)
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .nunique()
    )


def count_unique_delayed_parts(
    unique_df,
):
    """
    Count unique parts classified as RED - PRODUCTION.
    """

    if unique_df.empty:
        return 0

    return int(
        (
            unique_df["CALC_CLASSIFICATION"]
            == "RED - PRODUCTION"
        ).sum()
    )


def count_unique_by_classification(
    unique_df,
):
    """
    Return unique part count by NPI classification.
    """

    if unique_df.empty:
        return pd.Series(dtype="int64")

    return (
        unique_df[
            "CALC_CLASSIFICATION"
        ]
        .value_counts()
    )


def count_unique_any_red(
    unique_df,
):
    """
    Count unique parts with any RED classification.
    """

    if unique_df.empty:
        return 0

    return int(
        unique_df[
            "CALC_CLASSIFICATION"
        ]
        .astype(str)
        .str.startswith("RED")
        .sum()
    )


# ============================================================
# AGING BUCKET
# ============================================================

def get_aging_bucket(days):
    """
    Standard NPI aging buckets:

        <30 days
        30-90 days
        3-6 months
        6-12 months
        1-2 years
        >2 years
    """

    if pd.isna(days):
        return ""

    try:
        value = float(days)
    except Exception:
        return ""

    if value < 30:
        return "<30 days"

    if value <= 90:
        return "30-90 days"

    if value <= 180:
        return "3-6 months"

    if value <= 365:
        return "6-12 months"

    if value <= 730:
        return "1-2 years"

    return ">2 years"


# ============================================================
# ADD STAGE-SPECIFIC AGING
# ============================================================

def add_aging_fields(
    unique_df,
    control_date=None,
):
    """
    Add stage-specific aging fields.

    Engineering:
        Order Age

    Process:
        Order Age

    Production:
        Delay Days
    """

    data = unique_df.copy()

    data["CALC_ORDER_AGE_BUCKET"] = data[
        "CALC_ORDER_AGE_DAYS"
    ].apply(
        get_aging_bucket
    )

    data["CALC_PRODUCTION_DELAY_BUCKET"] = data[
        "CALC_PRODUCTION_DELAY_AGE"
    ].apply(
        get_aging_bucket
    )

    # --------------------------------------------------------
    # One management-facing aging field
    # --------------------------------------------------------

    def stage_aging(row):

        classification = _clean_text(
            row.get("CALC_CLASSIFICATION")
        )

        if classification == "RED - ENGINEERING":
            return row.get(
                "CALC_ORDER_AGE_DAYS"
            )

        if classification == "RED - PROCESS":
            return row.get(
                "CALC_ORDER_AGE_DAYS"
            )

        if classification == "RED - PRODUCTION":
            return row.get(
                "CALC_PRODUCTION_DELAY_AGE"
            )

        return pd.NA

    data["CALC_STAGE_AGING_DAYS"] = data.apply(
        stage_aging,
        axis=1,
    )

    data["CALC_STAGE_AGING_BUCKET"] = data.apply(
        lambda row: get_aging_bucket(
            row.get("CALC_STAGE_AGING_DAYS")
        ),
        axis=1,
    )

    return data


# ============================================================
# PRODUCTION HOTSPOT
# ============================================================

def build_production_hotspot(
    unique_df,
):
    """
    Build Production Hotspot table.

    Only unique parts classified as:
        RED - PRODUCTION

    are included.

    Grouping basis:
        Current Operation

    Output:
        Current Operation | Unique Parts
    """

    if unique_df.empty:
        return pd.DataFrame(
            columns=[
                "CURRENT_OPERATION",
                "UNIQUE_PARTS",
            ]
        )

    production = unique_df[
        unique_df["CALC_CLASSIFICATION"]
        == "RED - PRODUCTION"
    ].copy()

    if production.empty:
        return pd.DataFrame(
            columns=[
                "CURRENT_OPERATION",
                "UNIQUE_PARTS",
            ]
        )

    hotspot = (
        production
        .assign(
            CURRENT_OPERATION=
            production[
                "CALC_CURRENT_OPERATION"
            ]
            .fillna("")
            .astype(str)
            .str.strip()
        )
        .groupby(
            "CURRENT_OPERATION",
            dropna=False,
        )
        .size()
        .reset_index(
            name="UNIQUE_PARTS"
        )
        .sort_values(
            "UNIQUE_PARTS",
            ascending=False,
            kind="mergesort",
        )
        .reset_index(drop=True)
    )

    return hotspot


# ============================================================
# CUSTOMER SUMMARY
# ============================================================

def get_customer_summary(
    unique_df,
):
    """
    Unique-part classification by customer.
    """

    if unique_df.empty:
        return pd.DataFrame()

    summary = (
        unique_df
        .pivot_table(
            index="CALC_CUSTOMER",
            columns="CALC_CLASSIFICATION",
            values=PART_COLUMN,
            aggfunc="count",
            fill_value=0,
        )
        .reset_index()
    )

    return summary


# ============================================================
# SCAN GAP SUMMARY
# ============================================================

def get_scan_gap_summary(
    df,
):
    """
    Summary of scan-gap diagnostics.
    """

    if df.empty:
        return pd.DataFrame(
            columns=[
                "SCAN_GAP_PROCESS",
                "UNIQUE_PARTS",
            ]
        )

    gaps = df[
        pd.to_numeric(
            df["CALC_MISSED_SCAN_COUNT"],
            errors="coerce",
        ).fillna(0)
        > 0
    ].copy()

    if gaps.empty:
        return pd.DataFrame(
            columns=[
                "SCAN_GAP_PROCESS",
                "UNIQUE_PARTS",
            ]
        )

    summary = (
        gaps
        .groupby(
            "CALC_SCAN_GAP_PROCESS",
            dropna=False,
        )[PART_COLUMN]
        .nunique()
        .reset_index(
            name="UNIQUE_PARTS"
        )
        .sort_values(
            "UNIQUE_PARTS",
            ascending=False,
        )
        .reset_index(drop=True)
    )

    return summary


# ============================================================
# CALCULATION SUMMARY
# ============================================================

def get_calculation_summary(
    calculation_df,
    unique_df=None,
):
    """
    Return core calculation and unique-part metrics.
    """

    if unique_df is None:
        unique_df = build_unique_part_dataset(
            calculation_df
        )

    classification_counts = (
        unique_df[
            "CALC_CLASSIFICATION"
        ]
        .value_counts()
        .to_dict()
    )

    return {
        "CALCULATION_RECORDS": len(
            calculation_df
        ),

        "UNIQUE_PARTS": len(
            unique_df
        ),

        "RED_ENGINEERING": classification_counts.get(
            "RED - ENGINEERING",
            0,
        ),

        "RED_PROCESS": classification_counts.get(
            "RED - PROCESS",
            0,
        ),

        "RED_PRODUCTION": classification_counts.get(
            "RED - PRODUCTION",
            0,
        ),

        "GREEN_ON_TRACK": classification_counts.get(
            "GREEN - ON TRACK",
            0,
        ),

        "GREEN_COMPLETE": classification_counts.get(
            "GREEN - COMPLETE",
            0,
        ),

        "RED_ANY": count_unique_any_red(
            unique_df
        ),
    }
