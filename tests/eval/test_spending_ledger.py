import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from ukrainian_llm_eval.spending_ledger import (
    SharedSpendingLedger,
    SpendingCapExceeded,
    SpendingLedgerError,
)


def _reserve(path, reservation_id, amount):
    ledger = SharedSpendingLedger(path, ledger_id="issue5-public-evaluator", cap_micro_usd=100)
    return ledger.reserve(
        reservation_id,
        {"attempt_id": reservation_id},
        maximum_micro_usd=amount,
        funding_kind="metered",
        account_sha256="a" * 64,
    )


def test_concurrent_execution_roots_share_one_atomic_cap(tmp_path):
    path = tmp_path / "shared" / "spending.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_reserve, path, f"reserve-{index}", 60) for index in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result()["state"])
        except SpendingCapExceeded:
            outcomes.append("cap_exceeded")
    assert sorted(outcomes) == ["cap_exceeded", "unresolved"]
    snapshot = SharedSpendingLedger(
        path, ledger_id="issue5-public-evaluator", cap_micro_usd=100
    ).snapshot()
    assert snapshot["unresolved_new_spend_micro_usd"] == 60
    assert snapshot["remaining_new_spend_micro_usd"] == 40


def test_database_and_trusted_parent_are_owner_only_under_permissive_umask(tmp_path):
    path = tmp_path / "shared-private" / "spending.sqlite3"
    previous = os.umask(0)
    try:
        SharedSpendingLedger(path, ledger_id="issue5-public-evaluator", cap_micro_usd=100)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_timeout_retains_full_reservation_until_authoritative_settlement(tmp_path):
    path = tmp_path / "spending.sqlite3"
    _reserve(path, "reserve-timeout", 80)
    restarted = SharedSpendingLedger(path, ledger_id="issue5-public-evaluator", cap_micro_usd=100)
    with pytest.raises(SpendingCapExceeded):
        restarted.reserve(
            "reserve-next",
            {"attempt_id": "next"},
            maximum_micro_usd=21,
            funding_kind="metered",
            account_sha256="a" * 64,
        )
    assert restarted.get("reserve-timeout")["state"] == "unresolved"


def test_authoritative_settlement_releases_only_unused_and_replays_exactly(tmp_path):
    path = tmp_path / "spending.sqlite3"
    ledger = SharedSpendingLedger(path, ledger_id="issue5-public-evaluator", cap_micro_usd=100)
    reservation = _reserve(path, "reserve-one", 80)
    settled = ledger.settle("reserve-one", charged_micro_usd=25, evidence_sha256="b" * 64)
    assert settled["state"] == "settled"
    assert ledger.snapshot()["remaining_new_spend_micro_usd"] == 75
    replay = ledger.settle("reserve-one", charged_micro_usd=25, evidence_sha256="b" * 64)
    assert replay["replayed"] is True
    with pytest.raises(SpendingLedgerError, match="settlement drift"):
        ledger.settle("reserve-one", charged_micro_usd=24, evidence_sha256="b" * 64)
    assert reservation["reservation_sha256"] != settled["reservation_sha256"]


def test_ledger_identity_and_cap_cannot_reset_at_new_execution_root(tmp_path):
    path = tmp_path / "spending.sqlite3"
    SharedSpendingLedger(path, ledger_id="issue5-public-evaluator", cap_micro_usd=100)
    with pytest.raises(SpendingLedgerError, match="identity or cap drift"):
        SharedSpendingLedger(path, ledger_id="issue5-public-evaluator", cap_micro_usd=101)
    with pytest.raises(SpendingLedgerError, match="identity or cap drift"):
        SharedSpendingLedger(path, ledger_id="other-experiment", cap_micro_usd=100)


def test_existing_credit_stays_retained_until_balance_reconciliation(tmp_path):
    ledger = SharedSpendingLedger(
        tmp_path / "spending.sqlite3", ledger_id="issue5-public-evaluator", cap_micro_usd=100
    )
    ledger.reserve(
        "credit-one", {"attempt_id": "one"}, maximum_micro_usd=80,
        funding_kind="existing_credit", account_sha256="c" * 64,
        credit_available_micro_usd=100,
    )
    ledger.settle("credit-one", charged_micro_usd=30, evidence_sha256="d" * 64)
    with pytest.raises(SpendingCapExceeded, match="existing-credit"):
        ledger.reserve(
            "credit-two", {"attempt_id": "two"}, maximum_micro_usd=71,
            funding_kind="existing_credit", account_sha256="c" * 64,
            credit_available_micro_usd=100,
        )
    ledger.reconcile_existing_credit("credit-one", evidence_sha256="e" * 64)
    assert ledger.reserve(
        "credit-two", {"attempt_id": "two"}, maximum_micro_usd=71,
        funding_kind="existing_credit", account_sha256="c" * 64,
        credit_available_micro_usd=100,
    )["state"] == "unresolved"
