"""Synthetic provider/credential controls; no live calls or scored inputs."""
import copy
import hashlib
import importlib
import sys
import urllib.error
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ukrainian_llm_eval.admission import validate_admission_result
from ukrainian_llm_eval.admission_command import invoke_admission
from ukrainian_llm_eval.spending_ledger import SharedSpendingLedger

PROBES = Path(__file__).resolve().parents[2] / 'tools' / 'admission'
sys.path.insert(0, str(PROBES))
probe = importlib.import_module('gemma_probe')
common = importlib.import_module('probe_common')
TOKEN = 'synthetic-token-not-a-credential'
ACCOUNT = common.digest({'provider': 'openrouter', 'creator_user_id': 'synthetic-user'})


def rehash(request):
    request['request_sha256'] = common.digest({k: v for k, v in request.items() if k != 'request_sha256'})
    return request


def request():
    return rehash({'schema': 'ukrainian-llm-eval.admission-request.v1', 'nonce': 'a' * 32,
                  'requested_at': common.utcnow(), 'route_sha256': 'b' * 64, 'model': probe.MODEL,
                  'effort': None, 'condition': 'sources', 'composite_sha256': 'c' * 64,
                  'requirements': {'input_utf8_bytes': 100, 'max_total_input_tokens': 5_376_000,
                                   'max_total_output_tokens': 172_032, 'max_output_tokens': 4096,
                                   'max_tool_calls': 20, 'timeout_seconds': 300,
                                   'tool_policy_sha256': 'd' * 64}})


def config():
    return {'support': support(), 'provider': 'openrouter', 'model': probe.MODEL, 'effort': None,
            'backend': probe.BACKEND, 'expected_provider_name': 'Venice', 'reasoning_enabled': False,
            'account_scope': 'personal_provider_user', 'key_env': 'SYNTHETIC_GEMMA_KEY',
            'credential_sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
            'maximum_segment_micro_usd': 707_052,
            'pricing': {'route_sha256': 'b' * 64, 'currency': 'USD',
                        'input_micro_usd_per_million_tokens': 120_000,
                        'output_micro_usd_per_million_tokens': 360_000, 'tool_round_micro_usd': 0},
            'entitlement': {'route_sha256': 'b' * 64, 'account_sha256': ACCOUNT, 'billing_kind': 'metered',
                            'zero_incremental': False,
                            'valid_until': (datetime.now(UTC) + timedelta(days=1)).isoformat()},
            'capability': {'route_sha256': 'b' * 64, 'model': probe.MODEL, 'effort': None,
                           'context_input_tokens': 247_808, 'max_output_tokens': 8192,
                           'max_tool_calls': 20, 'timeout_seconds': 600, 'tool_policy_sha256': 'd' * 64}}


def support():
    return {'conditions': ['closed_book', 'sources'], 'framing_tokens': 1000, 'initial_history_tokens': 1000,
            'initial_history_verified': True, 'output_headroom_verified': True,
            'context_window_tokens': 256_000, 'output_headroom_tokens': 8192}


def responses():
    return {probe.KEY_URL: {'creator_user_id': 'synthetic-user', 'is_free_tier': False,
                           'is_management_key': False, 'is_provisioning_key': False,
                           'expires_at': (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                           'limit': None, 'limit_remaining': None},
            probe.CREDITS_URL: {'total_credits': '2', 'total_usage': '0.1'},
            probe.MODEL_URL: {'id': probe.MODEL, 'endpoints': [{
                'tag': probe.BACKEND, 'provider_name': 'Venice', 'model_id': probe.MODEL,
                'quantization': 'bf16', 'status': 0, 'context_length': 256_000, 'max_completion_tokens': 8192,
                'max_prompt_tokens': None,
                'supported_parameters': ['tools', 'structured_outputs', 'reasoning', 'max_tokens'],
                'pricing': {'prompt': '0.00000012', 'completion': '0.00000036',
                            'input_cache_read': '0.00000009', 'discount': 0}}]}}


def install_responses(monkeypatch, data):
    calls = []

    def provider_json(url, token=None):
        assert token == (None if url == probe.MODEL_URL else TOKEN)
        calls.append(url)
        return copy.deepcopy(data[url])

    monkeypatch.setenv('SYNTHETIC_GEMMA_KEY', TOKEN)
    monkeypatch.setattr(probe, 'provider_json', provider_json)
    return calls


def test_real_contract_result_binds_nonce_user_and_metered_eligibility(monkeypatch):
    cfg, req = config(), request()
    calls = install_responses(monkeypatch, responses())
    observed = probe.collect(cfg)
    result = probe.build_result(req, cfg, support(), observed,
                                {'unresolved_new_spend_micro_usd': 0, 'remaining_new_spend_micro_usd': 9_000_000})
    assert calls == [probe.KEY_URL, probe.CREDITS_URL, probe.MODEL_URL]
    assert result['entitlement']['observed'] == {'eligible': True, 'credit_available_micro_usd': None}
    assert result['entitlement']['state']['account_sha256'] == ACCOUNT
    assert TOKEN not in common.canonical(result).decode()
    assert 'synthetic-user' not in common.canonical(result).decode()
    assert '"available_micro_usd"' not in common.canonical(result).decode()
    authorization = {'schema': 'ukrainian-llm-eval.operator-authorization.v1', 'route_sha256': 'b' * 64,
                     'allow_paid': True, 'max_new_spend_micro_usd': cfg['maximum_segment_micro_usd']}
    route = {'route_sha256': 'b' * 64, 'conditions': ['sources'],
             'billing': {'kind': 'metered', **{k: cfg['pricing'][k] for k in (
                 'input_micro_usd_per_million_tokens', 'output_micro_usd_per_million_tokens', 'tool_round_micro_usd')}},
             'pricing_evidence_sha256': common.digest(cfg['pricing']),
             'entitlement_evidence_sha256': common.digest(cfg['entitlement']),
             'capability_evidence_sha256': common.digest(cfg['capability']),
             'operator_authorization_sha256': common.digest(authorization)}
    receipt = validate_admission_result(result, req, route, {'model': probe.MODEL, 'effort': None},
                                       reserved_micro_usd=cfg['maximum_segment_micro_usd'],
                                       remaining_ceiling_micro_usd=9_000_000,
                                       operator_authorization=authorization, max_age_seconds=300)
    assert receipt['account_sha256'] == ACCOUNT


@pytest.mark.parametrize('field,value', [('nonce', 'bad'), ('effort', 'low'), ('model', 'other'),
                                        ('condition', 'unselected')])
def test_invalid_request_rejected(field, value):
    req = request()
    req[field] = value
    with pytest.raises(common.ProbeError):
        probe.validate_request(rehash(req))


def test_frozen_gec_output_is_rejected_without_provider_calls(monkeypatch):
    req = request()
    req['requirements']['max_output_tokens'] = 16384
    calls = install_responses(monkeypatch, responses())
    with pytest.raises(common.ProbeError, match='capacity_insufficient'):
        probe.requirements_for(rehash(req), config(), support())
    assert calls == []


@pytest.mark.parametrize('field,value', [('creator_user_id', ''), ('creator_user_id', 'other-user'),
                                        ('is_management_key', True), ('is_free_tier', None),
                                        ('is_provisioning_key', True), ('expires_at', None),
                                        ('expires_at', '2000-01-01T00:00:00+00:00')])
def test_identity_or_key_eligibility_unknown_rejects(monkeypatch, field, value):
    data = responses()
    data[probe.KEY_URL][field] = value
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError):
        probe.collect(config())


def test_wrong_credential_rejects_before_http(monkeypatch):
    calls = install_responses(monkeypatch, responses())
    monkeypatch.setenv('SYNTHETIC_GEMMA_KEY', 'different-token')
    with pytest.raises(common.ProbeError, match='credential_identity_mismatch'):
        probe.collect(config())
    assert calls == []


@pytest.mark.parametrize('field,value', [('tag', 'other/backend'), ('model_id', 'other-model'),
                                        ('provider_name', 'other-provider'), ('status', 1),
                                        ('context_length', None), ('max_completion_tokens', 16384),
                                        ('supported_parameters', ['max_tokens'])])
def test_backend_capacity_drift_rejects(monkeypatch, field, value):
    data = responses()
    data[probe.MODEL_URL]['endpoints'][0][field] = value
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError):
        probe.collect(config())


@pytest.mark.parametrize('prices', [{'prompt': '0.00000013', 'completion': '0.00000036'},
                                    {'prompt': '0.00000012', 'completion': '0.00000036', 'request': '0.01'},
                                    {'prompt': '0.00000012', 'completion': '0.00000036', 'unknown_fee': '0'},
                                    {'prompt': '0.00000012', 'completion': '0.00000036', 'discount': '-1'}])
def test_changed_prices_or_unknown_fees_reject(monkeypatch, prices):
    data = responses()
    data[probe.MODEL_URL]['endpoints'][0]['pricing'] = prices
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError):
        probe.collect(config())


@pytest.mark.parametrize('key_limit,funds,unresolved,remaining,accepted', [
    (None, '0.707052', 0, 707052, True),
    (None, '0.707051', 0, 9_000_000, False),
    (None, '0.707052', 1, 9_000_000, False),
    (None, '0.707053', 1, 9_000_000, True),
    ('0.1', '2', 0, 9_000_000, False),
    (None, '2', 0, 707051, False),
])
def test_current_funds_key_limit_and_all_outstanding_commitments(
    monkeypatch, key_limit, funds, unresolved, remaining, accepted,
):
    data = responses()
    data[probe.CREDITS_URL] = {'total_credits': funds, 'total_usage': 0}
    data[probe.KEY_URL].update(limit=key_limit, limit_remaining=key_limit)
    install_responses(monkeypatch, data)
    cfg, req = config(), request()
    observed = probe.collect(cfg)
    if accepted:
        assert probe.build_result(req, cfg, support(), observed,
                                  {'unresolved_new_spend_micro_usd': unresolved,
                                   'remaining_new_spend_micro_usd': remaining})['entitlement']['observed']['eligible']
    else:
        with pytest.raises(common.ProbeError, match='next_reservation_unfunded'):
            probe.build_result(req, cfg, support(), observed,
                               {'unresolved_new_spend_micro_usd': unresolved,
                                'remaining_new_spend_micro_usd': remaining})


def test_fractional_usage_cannot_invent_spendable_micro_usd(monkeypatch):
    data = responses()
    data[probe.CREDITS_URL] = {'total_credits': '0.7070521', 'total_usage': '0.0000002'}
    install_responses(monkeypatch, data)
    assert probe.collect(config())['available_micro_usd'] == 707051


@pytest.mark.parametrize('case', ['redirect', 'oversized', 'duplicate', 'nan', '403', 'cached'])
def test_http_failure_is_bounded_and_never_echoes_provider_data(monkeypatch, case):
    class Response:
        status = 200
        headers = {'Age': '1'} if case == 'cached' else {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return 'https://example.invalid' if case == 'redirect' else probe.KEY_URL

        def read(self, limit):
            assert limit == common.MAX_BYTES + 1
            return {'oversized': b'x' * limit, 'duplicate': b'{"data":{},"data":{}}',
                    'nan': b'{"data":{"balance":NaN}}'}.get(case, b'{"data":{}}')

    class Opener:
        def open(self, req, timeout):
            assert req.method == 'GET' and timeout == 15
            assert req.headers['Authorization'] == 'Bearer ' + TOKEN
            if case == '403':
                raise urllib.error.HTTPError(probe.KEY_URL, 403, TOKEN, {}, None)
            return Response()

    def build_opener(proxy, redirect):
        assert isinstance(proxy, urllib.request.ProxyHandler) and proxy.proxies == {}
        assert isinstance(redirect, common.NoRedirect)
        return Opener()

    monkeypatch.setattr(urllib.request, 'build_opener', build_opener)
    with pytest.raises(common.ProbeError) as exc:
        probe.provider_json(probe.KEY_URL, TOKEN)
    assert TOKEN not in str(exc.value)


def test_http_decimal_precision_is_preserved(monkeypatch):
    class Response:
        status = 200

        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return probe.CREDITS_URL

        def read(self, limit):
            return b'{"data":{"total_credits":1.123456789012345678,"total_usage":0}}'

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    assert probe.provider_json(probe.CREDITS_URL, TOKEN)['total_credits'] == Decimal('1.123456789012345678')


def snapshot_spec(tmp_path, monkeypatch, case):
    """Package real public ledger code in a declared pure-Python wheel fixture."""
    cfg = config()
    binary = tmp_path / 'native-binary'
    binary.write_bytes(b'synthetic-native-runtime')
    artifact = tmp_path / 'reviewed-support.json'
    artifact.write_bytes(b'{"synthetic_control_proof":true}\n')
    wheel = tmp_path / 'ukrainian_llm_eval-0.1.0.dev0-py3-none-any.whl'
    package = PROBES.parents[1] / 'src' / 'ukrainian_llm_eval'
    with zipfile.ZipFile(wheel, 'w') as archive:
        for source in package.glob('*.py'):
            archive.write(source, 'ukrainian_llm_eval/' + source.name)
        archive.writestr('ukrainian_llm_eval-0.1.0.dev0.dist-info/WHEEL',
                         'Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('ukrainian_llm_eval-0.1.0.dev0.dist-info/METADATA',
                         'Metadata-Version: 2.1\nName: ukrainian-llm-eval\nVersion: 0.1.0.dev0\n')
    cfg.update(binary=str(binary), runtime_files=[{'path': str(binary), 'byte_sha256': common.file_hash(binary)}],
               budget_wheel={'name': wheel.name, 'byte_sha256': common.file_hash(wheel)})
    cfg['support'] = {**support(), **{k: cfg[k] for k in (
        'provider', 'model', 'backend', 'reasoning_enabled', 'account_scope', 'credential_sha256')},
        'runtime_files_sha256': common.digest(cfg['runtime_files']),
        'pricing_sha256': common.digest(cfg['pricing']), 'capability_sha256': common.digest(cfg['capability']),
        'artifacts': [{'name': artifact.name, 'byte_sha256': common.file_hash(artifact)}],
        **{claim: True for claim in (
            'personal_key_ownership_verified', 'same_credential_native_execution_verified',
            'native_control_enforcement_verified', 'provider_routing_and_price_caps_verified',
            'byte_token_upper_bound_verified', 'all_non_token_fees_excluded')}}
    ledger_path = tmp_path / 'shared' / 'spending.sqlite3'
    if case != 'missing_ledger':
        ledger = SharedSpendingLedger(ledger_path, ledger_id='synthetic-gemma', cap_micro_usd=10_000_000)
        ledger.reserve('old-unknown-user', {}, maximum_micro_usd=111,
                       funding_kind='metered', account_sha256='f' * 64)
    cfg['ledger'] = {'path': str(ledger_path), 'ledger_id': 'synthetic-gemma', 'cap_micro_usd': 10_000_000}
    if case == 'wheel_drift':
        cfg['budget_wheel']['byte_sha256'] = '0' * 64
    if case == 'unsupported_proof':
        cfg['support']['byte_token_upper_bound_verified'] = False
    data = responses()
    if case == 'outstanding_unfunded':
        data[probe.CREDITS_URL] = {'total_credits': '0.707052', 'total_usage': 0}
    response_file = tmp_path / 'provider-responses.json'
    response_file.write_bytes(common.canonical(data))
    config_file = tmp_path / 'gemma-config.json'
    config_file.write_bytes(common.canonical(cfg))
    wrapper = tmp_path / 'synthetic_http_only.py'
    wrapper.write_text('''import os, sys
from pathlib import Path
import gemma_probe
from probe_common import fail, parse
assert 'UNDECLARED_SECRET' not in os.environ
responses = parse((Path(__file__).parent / 'provider-responses.json').read_bytes())
calls = []
def fake_get(url, token=None):
    assert url in {gemma_probe.KEY_URL, gemma_probe.CREDITS_URL, gemma_probe.MODEL_URL}
    assert token == (None if url == gemma_probe.MODEL_URL else os.environ['SYNTHETIC_GEMMA_KEY'])
    calls.append(url)
    return responses[url]
gemma_probe.provider_json = fake_get
result = gemma_probe.main()
assert not any(name in sys.modules for name in ('ukrainian_llm_eval.execution', 'ukrainian_llm_eval.request_budget',
                                               'ukrainian_llm_eval.evidence'))
if result == 0:
    assert calls == [gemma_probe.KEY_URL, gemma_probe.CREDITS_URL, gemma_probe.MODEL_URL]
raise SystemExit(result)
''')
    executable = Path(sys.executable).resolve()
    declarations = [(executable, 'executable'), (wrapper, 'script'), (config_file, 'runtime_lock'),
                    (PROBES / 'gemma_probe.py', 'dependency'), (PROBES / 'probe_common.py', 'dependency'),
                    (wheel, 'dependency'), (artifact, 'dependency'), (response_file, 'dependency')]
    spec = {'schema': 'ukrainian-llm-eval.admission-command.v1', 'runtime': 'python-script-v1',
            'argv': [str(executable), str(wrapper), str(config_file)],
            'declared_files': [{'path': str(path), 'byte_sha256': common.file_hash(path), 'role': role}
                               for path, role in declarations],
            'env_names': ['SYNTHETIC_GEMMA_KEY'], 'timeout_seconds': 15,
            'stdin_max_bytes': 20_000, 'stdout_max_bytes': 20_000, 'stderr_max_bytes': 20_000}
    monkeypatch.setenv('SYNTHETIC_GEMMA_KEY', TOKEN)
    monkeypatch.setenv('UNDECLARED_SECRET', 'must-not-reach-child')
    return spec, ledger_path


def ledger_state(path):
    return {p.name: (p.stat().st_mode, p.stat().st_mtime_ns, common.file_hash(p))
            for p in path.parent.glob('spending.sqlite3*')} if path.parent.exists() else {}


@pytest.mark.parametrize('case,expected', [('valid', 'success'), ('missing_ledger', 'nonzero_exit'),
                                         ('wheel_drift', 'nonzero_exit'), ('unsupported_proof', 'nonzero_exit'),
                                         ('outstanding_unfunded', 'nonzero_exit')])
def test_actual_command_snapshot_uses_declared_wheel_without_ledger_writes(tmp_path, monkeypatch, case, expected):
    spec, ledger = snapshot_spec(tmp_path, monkeypatch, case)
    before = ledger_state(ledger)
    response = invoke_admission(spec, request())
    assert response['status'] == expected
    assert ledger_state(ledger) == before
    if case == 'missing_ledger':
        assert not ledger.parent.exists()
    if expected == 'success':
        result = common.parse(response['stdout'])
        assert result['schema'] == 'ukrainian-llm-eval.admission-result.v1'
        assert result['entitlement']['observed'] == {'eligible': True, 'credit_available_micro_usd': None}
        assert result['pricing']['observed']['conservative_segment_cost_micro_usd'] == 707052


def test_snapshot_rejects_changed_declared_dependency_before_execution(tmp_path, monkeypatch):
    spec, ledger = snapshot_spec(tmp_path, monkeypatch, 'valid')
    before = ledger_state(ledger)
    wheel = next(Path(row['path']) for row in spec['declared_files'] if row['path'].endswith('.whl'))
    wheel.write_bytes(b'changed dependency')
    response = invoke_admission(spec, request())
    assert response['status'] == 'identity_mismatch'
    assert ledger_state(ledger) == before


def test_ambient_package_is_not_used_as_a_budget_dependency(tmp_path):
    wheel = tmp_path / 'synthetic.whl'
    wheel.write_bytes(b'not-imported')
    cfg = {'budget_wheel': {'name': wheel.name, 'byte_sha256': common.file_hash(wheel)}}
    with pytest.raises(common.ProbeError, match='budget_runtime_not_isolated'):
        probe.read_commitments(cfg, tmp_path)


@pytest.mark.parametrize('field', ['limit', 'limit_remaining'])
def test_missing_limit_metadata_is_not_unlimited(monkeypatch, field):
    data = responses()
    del data[probe.KEY_URL][field]
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError, match='key_limit_unknown'):
        probe.collect(config())


def test_funding_permission_denial_does_not_seek_another_key(monkeypatch):
    calls = []
    data = responses()

    def restricted(url, token=None):
        assert token == TOKEN
        calls.append(url)
        if url == probe.CREDITS_URL:
            common.fail('provider_http_403')
        return data[url]

    monkeypatch.setenv('SYNTHETIC_GEMMA_KEY', TOKEN)
    monkeypatch.setattr(probe, 'provider_json', restricted)
    with pytest.raises(common.ProbeError, match='provider_http_403'):
        probe.collect(config())
    assert calls == [probe.KEY_URL, probe.CREDITS_URL]


@pytest.mark.parametrize('value', [None, True, '-1', 'NaN', 'Infinity', '1e100000', '1e-100000'])
def test_unknown_or_unbounded_money_is_rejected(value):
    with pytest.raises(common.ProbeError, match='provider_amount_invalid'):
        probe.amount(value)


def test_support_must_bind_selected_reasoning_mode(tmp_path, monkeypatch):
    spec, _ = snapshot_spec(tmp_path, monkeypatch, 'valid')
    path = next(Path(item['path']) for item in spec['declared_files'] if item['role'] == 'runtime_lock')
    cfg = common.parse(path.read_bytes())
    cfg['reasoning_enabled'] = True
    with pytest.raises(common.ProbeError, match='support_route_mismatch'):
        probe.support_for(cfg, tmp_path)


def test_input_bound_reserves_runtime_output_headroom():
    req, cfg, proof = request(), config(), support()
    proof['initial_history_tokens'] = 247_000
    with pytest.raises(common.ProbeError, match='input_does_not_fit'):
        probe.requirements_for(req, cfg, proof)


def test_initial_input_exact_net_boundary_does_not_subtract_output_twice():
    req, cfg, proof = request(), config(), support()
    proof['initial_history_tokens'] = 246_708
    assert probe.requirements_for(req, cfg, proof)[1] == 247_808
    proof['initial_history_tokens'] += 1
    with pytest.raises(common.ProbeError, match='input_does_not_fit'):
        probe.requirements_for(req, cfg, proof)


@pytest.mark.parametrize('field', ['initial_history_verified', 'output_headroom_verified'])
@pytest.mark.parametrize('value', [None, False, 1])
def test_initial_capacity_requires_explicit_proof_even_for_zero_history(field, value):
    proof = support()
    proof.update(initial_history_tokens=0)
    proof[field] = value
    with pytest.raises(common.ProbeError, match='support_proof_unavailable'):
        probe.requirements_for(request(), config(), proof)


@pytest.mark.parametrize('field', ['initial_history_tokens', 'framing_tokens',
                                  'context_window_tokens', 'output_headroom_tokens'])
def test_initial_capacity_requires_each_bound(field):
    proof = support()
    del proof[field]
    with pytest.raises(common.ProbeError):
        probe.requirements_for(request(), config(), proof)


@pytest.mark.parametrize('window,headroom', [(256_000, 4096), (8192, 8192), (255_999, 8192)])
def test_initial_capacity_rejects_unsafe_headroom(window, headroom):
    proof = support()
    proof.update(context_window_tokens=window, output_headroom_tokens=headroom)
    with pytest.raises(common.ProbeError, match='output_headroom_invalid'):
        probe.requirements_for(request(), config(), proof)


def test_legacy_history_budget_is_rejected():
    proof = support()
    proof['permitted_history_tokens'] = 0
    with pytest.raises(common.ProbeError, match='legacy_history_bound_rejected'):
        probe.requirements_for(request(), config(), proof)


def test_initial_input_also_fits_cumulative_billing_budget():
    req = request()
    req['requirements']['max_total_input_tokens'] = 2099
    with pytest.raises(common.ProbeError, match='input_does_not_fit'):
        probe.requirements_for(rehash(req), config(), support())


def novita_fixture():
    # Public endpoint metadata from the approved proposal; all funding is synthetic.
    cfg, data = config(), responses()
    cfg.update(backend='novita/bf16', expected_provider_name='Novita', maximum_segment_micro_usd=908_330)
    cfg['pricing'].update(input_micro_usd_per_million_tokens=140_000,
                          output_micro_usd_per_million_tokens=400_000)
    cfg['capability'].update(context_input_tokens=131_072, max_output_tokens=131_072)
    cfg['support'].update(context_window_tokens=262_144, output_headroom_tokens=131_072)
    endpoint = data[probe.MODEL_URL]['endpoints'][0]
    endpoint.update(tag='novita/bf16', provider_name='Novita', context_length=262_144,
                    max_completion_tokens=131_072)
    endpoint['pricing'].update(prompt='0.00000014', completion='0.00000040')
    return cfg, data


@pytest.mark.parametrize('output_limit', [4096, 16384])
def test_full_novita_metadata_fits_suite_limits(monkeypatch, output_limit):
    cfg, data = novita_fixture()
    req = request()
    req['requirements'].update(max_output_tokens=output_limit,
                               max_total_output_tokens=21 * output_limit)
    req = rehash(req)
    install_responses(monkeypatch, data)
    observed = probe.collect(cfg)
    result = probe.build_result(req, cfg, cfg['support'], observed,
                                {'unresolved_new_spend_micro_usd': 111,
                                 'remaining_new_spend_micro_usd': 9_000_000})
    assert result['entitlement']['observed']['eligible'] is True
    assert result['capability']['state']['max_output_tokens'] == 131_072
    assert result['pricing']['observed']['conservative_segment_cost_micro_usd'] == 908_330


@pytest.mark.parametrize('novita', [False, True])
@pytest.mark.parametrize('precision', [None, 'unknown', 'fp4', 'fp8', 'BF16'])
def test_endpoint_precision_must_be_explicit_bf16(monkeypatch, novita, precision):
    cfg, data = novita_fixture() if novita else (config(), responses())
    endpoint = data[probe.MODEL_URL]['endpoints'][0]
    if precision is None:
        endpoint.pop('quantization')
    else:
        endpoint['quantization'] = precision
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError, match='provider_precision_unverified'):
        probe.collect(cfg)


@pytest.mark.parametrize('field,value', [('tag', 'venice/bf16'), ('model_id', 'google/gemma-4-26b-a4b-it'),
                                        ('provider_name', 'Venice'), ('context_length', 256_000),
                                        ('max_completion_tokens', 16384)])
def test_novita_endpoint_identity_and_capacity_drift_reject(monkeypatch, field, value):
    cfg, data = novita_fixture()
    data[probe.MODEL_URL]['endpoints'][0][field] = value
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError):
        probe.collect(cfg)


@pytest.mark.parametrize('field,value', [('prompt', '0.00000015'), ('completion', '0.00000041')])
def test_novita_price_drift_rejects(monkeypatch, field, value):
    cfg, data = novita_fixture()
    data[probe.MODEL_URL]['endpoints'][0]['pricing'][field] = value
    install_responses(monkeypatch, data)
    with pytest.raises(common.ProbeError, match='provider_pricing_drift'):
        probe.collect(cfg)


@pytest.mark.parametrize('backend,provider,allowed', [('venice/bf16', 'Venice', True),
    ('novita/bf16', 'Novita', True), ('novita/bf16', 'Venice', False),
    ('venice/bf16', 'Novita', False), ('novita/fp8', 'Novita', False)])
def test_support_accepts_only_exact_approved_backend_provider_pairs(tmp_path, monkeypatch, backend, provider, allowed):
    spec, _ = snapshot_spec(tmp_path, monkeypatch, 'valid')
    path = next(Path(item['path']) for item in spec['declared_files'] if item['role'] == 'runtime_lock')
    cfg = common.parse(path.read_bytes())
    cfg.update(backend=backend, expected_provider_name=provider)
    cfg['support']['backend'] = backend
    if allowed:
        assert probe.support_for(cfg, tmp_path) == cfg['support']
    else:
        with pytest.raises(common.ProbeError, match='unsupported_route'):
            probe.support_for(cfg, tmp_path)
