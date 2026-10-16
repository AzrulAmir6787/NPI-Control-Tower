
from commitment_store import (
    _get_client,
    get_commitment,
    get_previous_commitment,
    save_commitment_patch,
)

TEST_KEY = "__NPI_COMMITMENT_PATCH_TEST__"

# Never overwrite an existing test record.
if get_commitment(TEST_KEY) is not None:
    raise RuntimeError(
        "Test record already exists. Inspect it before running again."
    )

if get_previous_commitment(TEST_KEY) is not None:
    raise RuntimeError(
        "Previous test record already exists. Inspect it before running again."
    )

client = _get_client()

try:
    # First save: set Owner and Remarks.
    save_commitment_patch(
        TEST_KEY,
        {
            "owner": "TEST_OWNER_1",
            "remarks": "TEST_REMARKS_1",
        },
    )

    first = get_commitment(TEST_KEY)

    assert first["owner"] == "TEST_OWNER_1"
    assert first["remarks"] == "TEST_REMARKS_1"
    assert first["version"] == 1

    # Second save: change Remarks only.
    save_commitment_patch(
        TEST_KEY,
        {"remarks": "TEST_REMARKS_2"},
    )

    second = get_commitment(TEST_KEY)
    previous = get_previous_commitment(TEST_KEY)

    assert second["owner"] == "TEST_OWNER_1"
    assert second["remarks"] == "TEST_REMARKS_2"
    assert second["version"] == 2

    assert previous["owner"] == "TEST_OWNER_1"
    assert previous["remarks"] == "TEST_REMARKS_1"
    assert previous["version"] == 1

    print("PASS: changing Remarks preserves Owner")
    print("PASS: Previous contains the complete prior record")

    # Third save: change Owner only.
    save_commitment_patch(
        TEST_KEY,
        {"owner": "TEST_OWNER_2"},
    )

    third = get_commitment(TEST_KEY)
    previous_again = get_previous_commitment(TEST_KEY)

    assert third["owner"] == "TEST_OWNER_2"
    assert third["remarks"] == "TEST_REMARKS_2"
    assert third["version"] == 3

    assert previous_again["owner"] == "TEST_OWNER_1"
    assert previous_again["remarks"] == "TEST_REMARKS_2"
    assert previous_again["version"] == 2

    print("PASS: changing Owner preserves Remarks")
    print("PASS: Previous advances to the immediately prior version")

finally:
    # Clean up only the dedicated test key.
    client.table("commitments_previous").delete().eq(
        "part_key", TEST_KEY
    ).execute()

    client.table("commitments").delete().eq(
        "part_key", TEST_KEY
    ).execute()

    print("Test records cleaned up.")