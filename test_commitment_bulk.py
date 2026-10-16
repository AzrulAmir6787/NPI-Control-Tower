
from commitment_store import (
    _get_client,
    get_commitment,
    get_commitments,
    get_previous_commitment,
    save_commitment_patch,
)

TEST_KEY = "__NPI_COMMITMENT_BULK_TEST__"
client = _get_client()

# Safety checks: never overwrite existing test records.
if get_commitment(TEST_KEY) is not None:
    raise RuntimeError("Test Latest record already exists. Stop.")

if get_previous_commitment(TEST_KEY) is not None:
    raise RuntimeError("Test Previous record already exists. Stop.")

try:
    # Create one temporary test record.
    save_commitment_patch(
        TEST_KEY,
        {
            "owner": "BULK_TEST_OWNER",
            "remarks": "BULK_TEST_REMARKS",
        },
    )

    # Read several keys in one function call.
    results = get_commitments([
        TEST_KEY,
        "__NPI_PART_THAT_DOES_NOT_EXIST__",
    ])

    assert TEST_KEY in results
    assert results[TEST_KEY]["owner"] == "BULK_TEST_OWNER"
    assert results[TEST_KEY]["remarks"] == "BULK_TEST_REMARKS"
    assert "__NPI_PART_THAT_DOES_NOT_EXIST__" not in results

    print("PASS: bulk read returns the saved commitment")
    print("PASS: missing part keys are safely omitted")

finally:
    # Clean up only the dedicated test key.
    client.table("commitments_previous").delete().eq(
        "part_key", TEST_KEY
    ).execute()

    client.table("commitments").delete().eq(
        "part_key", TEST_KEY
    ).execute()

    print("Test records cleaned up.")