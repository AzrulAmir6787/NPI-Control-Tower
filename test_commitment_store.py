
from commitment_store import (
    get_commitment,
    get_previous_commitment,
    save_commitment,
)

TEST_KEY = "__NPI_COMMITMENT_TEST_ONLY__"

# Safety check: do not overwrite an existing test record.
if get_commitment(TEST_KEY) is not None:
    raise RuntimeError(
        "Test record already exists. Stop and inspect it before proceeding."
    )

# First save: creates Latest version 1.
first = save_commitment(
    part_key=TEST_KEY,
    owner="TEST_OWNER_1",
    commitment_date=None,
    completion_date=None,
    remarks="TEST_REMARKS_1",
)

latest_1 = get_commitment(TEST_KEY)
previous_1 = get_previous_commitment(TEST_KEY)

assert latest_1 is not None
assert latest_1["version"] == 1
assert latest_1["owner"] == "TEST_OWNER_1"
assert latest_1["remarks"] == "TEST_REMARKS_1"
assert previous_1 is None

# Second save: moves the first Latest record to Previous.
second = save_commitment(
    part_key=TEST_KEY,
    owner="TEST_OWNER_2",
    commitment_date=None,
    completion_date=None,
    remarks="TEST_REMARKS_2",
)

latest_2 = get_commitment(TEST_KEY)
previous_2 = get_previous_commitment(TEST_KEY)

assert latest_2 is not None
assert previous_2 is not None

assert latest_2["version"] == 2
assert latest_2["owner"] == "TEST_OWNER_2"
assert latest_2["remarks"] == "TEST_REMARKS_2"

assert previous_2["version"] == 1
assert previous_2["owner"] == "TEST_OWNER_1"
assert previous_2["remarks"] == "TEST_REMARKS_1"

print("PASS: first save creates Latest version 1")
print("PASS: second save creates Latest version 2")
print("PASS: Previous contains the prior Latest record")
print("TEST RECORD REMAINS IN SUPABASE FOR INSPECTION")