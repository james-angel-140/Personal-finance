"""Setup-assistant digest building and proposal application (no API calls)."""

from src.ingest.csv_common import get_or_create_account, upsert_transaction
from src.setup_assistant import (
    HouseSetupProposal,
    ProposedBillGroup,
    ProposedContribution,
    ProposedHousemate,
    apply_proposal,
    build_digest,
)


def _add(db, txn_id, amount, counterparty, type_, posted_at):
    acct = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    upsert_transaction(db, {
        "id": txn_id,
        "account_id": acct,
        "posted_at": posted_at,
        "amount_pennies": amount,
        "counterparty": counterparty,
        "source": "csv_monzo",
        "raw_json": {"Type": type_},
    })
    db.commit()


def test_build_digest_groups_and_signals(db):
    # A housemate paying in across two months, plus a one-off merchant.
    _add(db, "in1", 32500, "Sam", "Faster payment", "2026-04-02T09:00:00")
    _add(db, "in2", 32500, "Sam", "Faster payment", "2026-05-02T09:00:00")
    _add(db, "shop", -1500, "Tesco", "Card payment", "2026-05-03T10:00:00")

    digest = build_digest(db)
    assert "Period: 2026-04 to 2026-05" in digest
    # Sam aggregated: 2 txns, 2 distinct months, £650 in
    assert "Sam" in digest
    sam_line = next(l for l in digest.splitlines() if "Sam" in l)
    assert "x2" in sam_line and "2 mo" in sam_line and "650.00" in sam_line
    assert "Tesco" in digest


def test_apply_proposal_writes_setup_and_is_idempotent(db):
    proposal = HouseSetupProposal(
        housemates=[
            ProposedHousemate(name="Sam", counterparties=["Sam"],
                              confidence="high", rationale="recurring"),
        ],
        bill_groups=[
            ProposedBillGroup(name="Rent", cadence="monthly", payees=["Landlord"],
                              typical_pounds=1000.0, your_share_pounds=350.0,
                              confidence="high", rationale="monthly DD"),
        ],
        contributions=[
            ProposedContribution(housemate_name="Sam", bill_group_name="Rent",
                                 expected_pounds=325.0),
        ],
        summary="confirm Sam's share",
    )

    counts = apply_proposal(db, proposal)
    assert counts == {"housemates": 1, "bill_groups": 1, "contributions": 1}

    # Pennies stored correctly, no float drift
    exp = db.execute("SELECT expected_pennies FROM contributions").fetchone()[0]
    assert exp == 32500

    # Re-applying must not duplicate rows
    apply_proposal(db, proposal)
    assert db.execute("SELECT COUNT(*) FROM housemates").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM contributions").fetchone()[0] == 1


def test_apply_skips_contribution_with_unknown_refs(db):
    proposal = HouseSetupProposal(
        housemates=[],
        bill_groups=[],
        contributions=[
            ProposedContribution(housemate_name="Ghost", bill_group_name="Rent",
                                 expected_pounds=100.0),
        ],
        summary="",
    )
    counts = apply_proposal(db, proposal)
    assert counts["contributions"] == 0
