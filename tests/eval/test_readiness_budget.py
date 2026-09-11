"""Synthetic admission-only budget observations preserve every existing byte."""

import copy
import hashlib
import sqlite3

import pytest
from test_request_budget import _provider_bound_values

from ukrainian_llm_eval.core import ExamError
from ukrainian_llm_eval.request_budget import RequestBudgetController
from ukrainian_llm_eval.spending_ledger import SharedSpendingLedger, SpendingLedgerError


def tree_state(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode, path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in root.rglob('*')
    }


@pytest.fixture
def prepared(tmp_path):
    route, _, original, _ = _provider_bound_values(tmp_path)
    controller = RequestBudgetController(
        original._raw_route_specs, shared_ledger_path=original._shared_ledger_path,
    )
    controller.prepare({
        'routes': [route], 'suites': [{'limits': {'max_tool_calls': 2, 'max_output_tokens': 100}}],
        'spending_policy': original._spending_policy,
    }, {'cells': []})
    return route, controller, original._shared_ledger


def test_existing_snapshot_preserves_unresolved_attempt_and_all_bytes(tmp_path, prepared, monkeypatch):
    _, controller, ledger = prepared
    before = tree_state(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail('readiness called a mutating or candidate allocation API')

    for method in ('__init__', '_initialize', 'reserve', 'settle', 'reconcile_existing_credit'):
        monkeypatch.setattr(SharedSpendingLedger, method, forbidden)
    monkeypatch.setattr('ukrainian_llm_eval.request_budget.request_budget_attempt_id', forbidden)
    monkeypatch.setattr('ukrainian_llm_eval.request_budget.EvidenceStore', forbidden)
    monkeypatch.setattr(controller, 'bind', forbidden)
    monkeypatch.setattr(controller, 'for_attempt', forbidden)
    snapshot = controller.inspect_readiness(tmp_path / 'run')
    assert snapshot['unresolved_new_spend_micro_usd'] == 606
    assert snapshot['remaining_new_spend_micro_usd'] == 394
    assert snapshot['reservation_count'] == 1
    assert len(snapshot['reservations_sha256']) == 64
    assert controller._store is None and controller._shared_ledger is None
    assert tree_state(tmp_path) == before
    assert ledger.get('reserve-provider-bound')['state'] == 'unresolved'


def test_next_capacity_is_sequential_not_entire_matrix(tmp_path, prepared):
    route, controller, ledger = prepared
    receipt = {'account_sha256': 'c' * 64, 'credit_available_micro_usd': None}
    with pytest.raises(ExamError, match='next worst-case'):
        controller.check_readiness_capacity(route, receipt, controller.inspect_readiness(tmp_path / 'run'))
    ledger.settle('reserve-provider-bound', charged_micro_usd=394, evidence_sha256='a' * 64)
    snapshot = controller.inspect_readiness(tmp_path / 'run')
    assert controller.check_readiness_capacity(route, receipt, snapshot) == {
        'maximum_micro_usd': 606, 'remaining_micro_usd': 606,
    }
    changed = copy.deepcopy(route)
    changed['billing']['max_total_input_tokens'] = 1
    with pytest.raises(ExamError, match='identity drift'):
        controller.check_readiness_capacity(changed, receipt, snapshot)


@pytest.mark.parametrize('drift', ['missing', 'empty', 'corrupt', 'identity', 'cap', 'legacy'])
def test_missing_corrupt_or_drifted_ledger_rejects_without_writes(tmp_path, drift):
    path = tmp_path / 'private' / 'ledger.sqlite3'
    if drift != 'missing':
        SharedSpendingLedger(path, ledger_id='fixture', cap_micro_usd=100)
        if drift in {'empty', 'corrupt'}:
            path.write_bytes(b'' if drift == 'empty' else b'not a database')
        elif drift == 'legacy':
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE ledger SET schema='legacy'")
    before = tree_state(tmp_path)
    with pytest.raises(SpendingLedgerError):
        SharedSpendingLedger.inspect_readiness(
            path, ledger_id='different' if drift == 'identity' else 'fixture',
            cap_micro_usd=101 if drift == 'cap' else 100,
        )
    assert tree_state(tmp_path) == before


def test_credit_conservation_and_reconciliation(tmp_path, prepared):
    route, controller, ledger = prepared
    route['billing']['kind'] = 'existing_credit'
    controller._routes[route['route_id']] = copy.deepcopy(route)
    for reservation_id, maximum in [('credit-unresolved', 40), ('credit-settled', 30), ('credit-reconciled', 20)]:
        ledger.reserve(reservation_id, {}, maximum_micro_usd=maximum,
                       funding_kind='existing_credit', account_sha256='c' * 64,
                       credit_available_micro_usd=100)
    ledger.settle('credit-settled', charged_micro_usd=25, evidence_sha256='a' * 64)
    ledger.settle('credit-reconciled', charged_micro_usd=15, evidence_sha256='b' * 64)
    ledger.reconcile_existing_credit('credit-reconciled', evidence_sha256='d' * 64)
    before = tree_state(tmp_path)
    snapshot = controller.inspect_readiness(tmp_path / 'run')
    assert snapshot['credit_commitments_micro_usd'] == {'c' * 64: 65}
    receipt = {'account_sha256': 'c' * 64, 'credit_available_micro_usd': 670}
    with pytest.raises(ExamError, match='next worst-case'):
        controller.check_readiness_capacity(route, receipt, snapshot)
    receipt['credit_available_micro_usd'] = 671
    assert controller.check_readiness_capacity(route, receipt, snapshot)['remaining_micro_usd'] == 606
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize('update', [
    "funding_kind='unknown'", "settled_micro_usd=1", "binding_sha256='bad'",
    "maximum_micro_usd=1001", "credit_reconciliation_sha256='bad'",
])
def test_invalid_outstanding_reservation_is_not_hidden(tmp_path, prepared, update):
    _, controller, ledger = prepared
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(f'UPDATE reservations SET {update}')
    before = tree_state(tmp_path)
    with pytest.raises(ExamError):
        controller.inspect_readiness(tmp_path / 'run')
    assert tree_state(tmp_path) == before


def test_wal_rejected_without_creating_sidecars(tmp_path):
    path = tmp_path / 'private' / 'ledger.sqlite3'
    SharedSpendingLedger(path, ledger_id='fixture', cap_micro_usd=100)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    conn.close()
    before = tree_state(tmp_path)
    with pytest.raises(SpendingLedgerError, match='rollback-journal'):
        SharedSpendingLedger.inspect_readiness(path, ledger_id='fixture', cap_micro_usd=100)
    assert tree_state(tmp_path) == before


def test_legacy_store_is_not_initialized_or_recovered(tmp_path):
    controller = RequestBudgetController({})
    controller.prepare({'routes': [{'route_id': 'free', 'request_budget_mechanism_sha256': None,
                                   'billing': {'kind': 'subscription'}}]}, {'cells': []})
    assert controller.inspect_readiness(tmp_path)['remaining_new_spend_micro_usd'] is None
    retained = tmp_path / 'request-budget-evidence'
    retained.mkdir()
    before = tree_state(tmp_path)
    with pytest.raises(ExamError, match='legacy'):
        controller.inspect_readiness(tmp_path)
    assert tree_state(tmp_path) == before


def test_inspection_uses_one_readonly_transaction(tmp_path, prepared, monkeypatch):
    _, controller, _ = prepared
    connect = sqlite3.connect
    statements = []
    connections = []

    def observing_connect(database, **kwargs):
        assert database.endswith('?mode=ro')
        assert kwargs == {'uri': True, 'isolation_level': None}
        connection = connect(database, **kwargs)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', observing_connect)
    controller.inspect_readiness(tmp_path / 'run')
    assert len(connections) == 1
    assert statements[0] == 'BEGIN'
    assert all(query.startswith(('BEGIN', 'SELECT', 'PRAGMA journal_mode', 'PRAGMA integrity_check'))
               for query in statements)
    assert all('=' not in query for query in statements if query.startswith('PRAGMA'))
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')
