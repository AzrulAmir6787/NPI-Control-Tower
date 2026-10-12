import pandas as pd


# ============================================================
# MANAGEMENT ACTION STORE
# ============================================================

ACTION_COLUMNS = [
    "ACTION_ID",
    "PART_NUMBER",
    "JOB_NUMBER",
    "CURRENT_OPERATION",
    "PROBLEM",
    "DELAY_DAYS",
    "ORDER_DATE",
    "OWNER",
    "ACTION",
    "SUPPORT_REQUIRED",
    "COMMITMENT_DATE",
    "ACTION_STATUS",
    "ACTUAL_COMPLETION",
    "REMARKS",
    "PRIORITY",
]


# ============================================================
# EMPTY ACTION STORE
# ============================================================

def create_empty_action_store():
    """
    Create an empty Management Action DataFrame.
    """

    return pd.DataFrame(
        columns=ACTION_COLUMNS
    )


# ============================================================
# COMMITMENT STATUS
# ============================================================

def calculate_commitment_status(
    commitment_date,
    action_status,
    control_date,
):
    """
    Management commitment status.

    CLOSED:
        COMPLETED

    No commitment date:
        NO COMMITMENT

    Commitment before Control Date:
        OVERDUE

    Commitment on Control Date:
        DUE TODAY

    Commitment after Control Date:
        UPCOMING
    """

    if (
        str(action_status)
        .strip()
        .upper()
        == "CLOSED"
    ):
        return "COMPLETED"

    if pd.isna(commitment_date):
        return "NO COMMITMENT"

    commitment = pd.to_datetime(
        commitment_date,
        errors="coerce",
    )

    control = pd.to_datetime(
        control_date,
        errors="coerce",
    )

    if pd.isna(commitment) or pd.isna(control):
        return "NO COMMITMENT"

    commitment = commitment.normalize()
    control = control.normalize()

    if commitment < control:
        return "OVERDUE"

    if commitment == control:
        return "DUE TODAY"

    return "UPCOMING"
