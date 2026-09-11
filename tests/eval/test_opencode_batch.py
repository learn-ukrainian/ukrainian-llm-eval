"""Offline singleton batch transport and native-gateway accounting proof."""
import copy
import hashlib
import io
import json
import time
from types import SimpleNamespace

import pytest

from ukrainian_llm_eval import adapters, native_opencode
from ukrainian_llm_eval import opencode_batch as batch
from ukrainian_llm_eval.opencode_gateway import OpenCodeGateway, decode_stream


def config():
    return native_opencode.validate_config({
        'schema': 'zno-nmt.config.v1', 'adapter': 'opencode', 'model': batch.MODEL, 'effort': None,
        'timeout_seconds': 15, 'max_output_tokens': 4096, 'max_tool_calls': 1, 'repeats': 1,
        'tools': [], 'corpus_id': None, 'provider': 'openrouter', 'endpoint_env': 'FIXTURE_ENDPOINT',
        'key_env': 'FIXTURE_KEY', 'openrouter': {'provider_endpoint': 'together',
            'expected_provider_name': 'Together', 'reasoning_enabled': False,
            'max_price': {'prompt': '0.39', 'completion': '0.97', 'request': '0'}}})


def payload():
    cfg = config()
    return {'model': batch.MODEL, 'messages': [{'role': 'user', 'content': 'Synthetic fixture'}],
        'max_tokens': 4096, 'stream': True, 'stream_options': {'include_usage': True}, 'usage': {'include': True},
        'reasoning': {'enabled': False}, 'provider': {'only': ['together'], 'allow_fallbacks': False,
            'require_parameters': True, 'max_price': cfg['openrouter']['max_price']},
        'tool_choice': 'required', 'tools': [{'type': 'function', 'function': {
            'name': 'StructuredOutput', 'parameters': {'type': 'object'}}}]}


def result(custom_id, provider='Together'):
    body = {'id': 'gen-batch-fixture', 'model': batch.BASE_MODEL, 'created': 1,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call-fixture', 'type': 'function', 'function': {'name': 'StructuredOutput',
                    'arguments': '{"responses":{"q1":"A"}}'}}]}, 'finish_reason': 'tool_calls'}]}
    if provider is not None:
        body['provider'] = provider
    return {'id': 'batch_fixture', 'endpoint': '/v1/chat/completions', 'model': batch.BASE_MODEL,
        'status': 'completed', 'error': None, 'request_counts': {'total': 1, 'completed': 1, 'failed': 0},
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'cost': 0.000009, 'is_byok': False},
        'results': [{'custom_id': custom_id, 'response': {'status_code': 200, 'body': body}, 'error': None}]}


class Budget:
    def __init__(self, events):
        self.events = events
        self.committed = []
        self.observed = []

    def commit_request(self, child):
        self.events.append('commit')
        self.committed.append(copy.deepcopy(child))
        return adapters.canonical(child).encode() + b'\n', {'synthetic': True}

    def observe(self, usage, *, tool_calls):
        self.events.append('observe')
        self.observed.append(copy.deepcopy(usage))
        assert tool_calls == 0
        return {'synthetic': True}


class Response(io.BytesIO):
    def __init__(self, url, value, status):
        super().__init__(adapters.canonical(value).encode())
        self.url, self.status = url, status

    def geturl(self):
        return self.url


def install(monkeypatch, events, mutation=None, missing_provider=False, generation_mutation=None, pending=False):
    seen, state = [], {}
    def send(request, timeout):
        assert request.headers['Authorization'] == 'Bearer synthetic-key'
        assert 0 < timeout <= 15
        seen.append(request)
        events.append(request.method)
        if request.method == 'POST':
            envelope = json.loads(request.data)
            assert list(envelope) == ['endpoint', 'model', 'requests']
            state['custom_id'] = envelope['requests'][0]['custom_id']
            return Response(request.full_url, {'id': 'batch_fixture', 'model': batch.BASE_MODEL,
                'endpoint': '/v1/chat/completions', 'status': 'validating'}, 202)
        if request.full_url.startswith(batch.GENERATION_URL):
            data = {'id': 'gen-batch-fixture', 'model': batch.BASE_MODEL,
                    'provider_name': 'Together', 'is_byok': False}
            if generation_mutation:
                generation_mutation(data)
            return Response(request.full_url, {'data': data}, 200)
        assert request.full_url == batch.BATCH_URL + '/batch_fixture'
        data = result(state['custom_id'], None if missing_provider else 'Together')
        if pending:
            data.update(status='in_progress', results=None)
        if mutation:
            mutation(data)
        return Response(request.full_url, data, 200)
    monkeypatch.setattr(batch.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=send))
    monkeypatch.setattr(batch.time, 'sleep', lambda seconds: None)
    return seen


def gateway(events, evidence):
    return OpenCodeGateway(config(), 'closed-book', endpoint='https://openrouter.ai/api/v1/chat/completions',
        key='synthetic-key', sources_url=None, evidence=lambda kind, value: evidence.append((kind, value)),
        request_budget=Budget(events), deadline=time.monotonic() + 15)


def test_singleton_native_transformation_commit_envelope_and_observation(monkeypatch):
    events, evidence = [], []
    seen = install(monkeypatch, events)
    with gateway(events, evidence) as g:
        native = payload()
        raw = g.completion(native)
        assert native['model'] == batch.MODEL and native['stream'] is True
        child = g.budget.committed[0]
        assert child == {k: v for k, v in {**native, 'model': batch.BASE_MODEL, 'stream': False}.items()
                         if k not in {'stream_options', 'usage'}}
        assert events == ['commit', 'POST', 'GET', 'observe']
        assert len(g.budget.observed) == 1
        decoded = decode_stream(raw)
        assert decoded['models'] == {batch.BASE_MODEL} and decoded['providers'] == {'Together'}
        assert decoded['calls'][0]['name'] == 'StructuredOutput'
        binding = dict(evidence)['opencode_batch_request_binding']
        assert binding['native_sha256'] == adapters.digest(native)
        assert binding['child_sha256'] == hashlib.sha256(adapters.canonical(child).encode() + b'\n').hexdigest()
        assert dict(evidence)['opencode_batch_envelope']['envelope_sha256'] == hashlib.sha256(seen[0].data).hexdigest()
        with pytest.raises(adapters.AdapterError):
            g.completion(native)
    assert len([r for r in seen if r.method == 'POST']) == 1


@pytest.mark.parametrize('mutation', [
    lambda x: x.update(id='batch_other'), lambda x: x.update(model=batch.MODEL),
    lambda x: x['results'][0].update(custom_id='other'), lambda x: x['results'].append(copy.deepcopy(x['results'][0])),
    lambda x: x['request_counts'].update(failed=1), lambda x: x['usage'].update(is_byok=True),
    lambda x: x['usage'].pop('cost'), lambda x: x['usage'].update(total_tokens=14),
    lambda x: x['results'][0]['response']['body'].update(provider='Google AI Studio'),
    lambda x: x['results'][0]['response']['body'].update(model=batch.MODEL),
    lambda x: x['results'][0]['response'].update(status_code=429), lambda x: x.update(status='failed'),
])
def test_batch_drift_or_unaccounted_failure_retains_commitment(monkeypatch, mutation):
    events, evidence = [], []
    seen = install(monkeypatch, events, mutation)
    with gateway(events, evidence) as g:
        with pytest.raises(adapters.AdapterError):
            g.completion(payload())
        assert len(g.budget.committed) == 1 and g.budget.observed == []
        with pytest.raises(adapters.AdapterError):
            g.completion(payload())
    assert len([r for r in seen if r.method == 'POST']) == 1


def test_missing_provider_resolved_only_from_correlated_generation(monkeypatch):
    events, evidence = [], []
    seen = install(monkeypatch, events, missing_provider=True)
    with gateway(events, evidence) as g:
        assert decode_stream(g.completion(payload()))['providers'] == {'Together'}
        assert len(g.budget.observed) == 1
    assert seen[-1].full_url == batch.GENERATION_URL + '?id=gen-batch-fixture'


@pytest.mark.parametrize('field,value', [('provider_name', None), ('provider_name', 'Other'),
    ('model', batch.MODEL), ('id', 'gen-other'), ('is_byok', True), ('is_byok', None)])
def test_generation_missing_or_drift_fails(monkeypatch, field, value):
    events, evidence = [], []
    install(monkeypatch, events, missing_provider=True, generation_mutation=lambda x: x.update({field: value}))
    with gateway(events, evidence) as g:
        with pytest.raises(adapters.AdapterError):
            g.completion(payload())
        assert not g.budget.observed


def test_pending_batch_timeout_does_not_resubmit_or_settle(monkeypatch):
    events, evidence = [], []
    seen = install(monkeypatch, events, pending=True)
    with gateway(events, evidence) as g:
        # Helper owns the immutable absolute deadline; move its clock beyond it.
        deadline = g.deadline
        monkeypatch.setattr(batch.time, 'sleep', lambda _: monkeypatch.setattr(batch.time, 'monotonic', lambda: deadline + 1))
        with pytest.raises(adapters.AdapterError, match='timeout'):
            g.completion(payload())
        assert not g.budget.observed
    assert [r.method for r in seen] == ['POST']


@pytest.mark.parametrize('endpoint', ['https://example.invalid/api/v1/chat/completions',
    'http://openrouter.ai/api/v1/chat/completions', 'https://openrouter.ai.evil/api/v1',
    'https://user@openrouter.ai/api/v1', 'https://openrouter.ai/api/v1?secret=x'])
def test_endpoint_rejected_before_commit_or_send(monkeypatch, endpoint):
    events, evidence = [], []
    seen = install(monkeypatch, events)
    with gateway(events, evidence) as g:
        g.endpoint = endpoint
        with pytest.raises(adapters.AdapterError, match='endpoint'):
            g.completion(payload())
        assert not g.budget.committed
    assert seen == []


@pytest.mark.parametrize('field,value', [('provider_endpoint', 'google-ai-studio'),
    ('expected_provider_name', 'Google AI Studio'),
    ('max_price', {'prompt': '0.4', 'completion': '0.97', 'request': '0'}),
    ('max_price', {'prompt': '0.39', 'completion': '0.98', 'request': '0'}),
    ('max_price', {'prompt': '0.39', 'completion': '0.97', 'request': '0.01'})])
def test_batch_native_config_rejects_provider_and_price_drift(field, value):
    cfg = config()
    cfg['openrouter'][field] = value
    with pytest.raises(adapters.AdapterError):
        native_opencode.validate_config(cfg)


def test_batch_native_config_capacity():
    cfg = config()
    cfg['max_output_tokens'] = 235929
    native_opencode.validate_config(cfg)
    value = native_opencode.native_config(cfg, 'closed-book', SimpleNamespace(allowed=set(), token='x', url='http://fixture'))
    assert value['provider']['openrouter']['models'][batch.MODEL]['limit']['context'] == 262144
    cfg['max_output_tokens'] += 1
    with pytest.raises(adapters.AdapterError):
        native_opencode.validate_config(cfg)


@pytest.mark.parametrize('failed', [False, True])
def test_real_request_budget_commits_child_and_preserves_failed_round(monkeypatch, tmp_path, failed):
    from test_request_budget import _values

    from ukrainian_llm_eval.evidence import EvidenceStore

    _, _, _, _, budget = _values(tmp_path, max_output=10000)
    budget.config['max_output_tokens'] = 4096
    events, evidence = [], []
    seen = install(monkeypatch, events, (lambda value: value.update(status='failed')) if failed else None)
    with gateway(events, evidence) as g:
        g.budget = budget
        if failed:
            with pytest.raises(adapters.AdapterError):
                g.completion(payload())
        else:
            g.completion(payload())
    assert budget.rounds == 1
    assert (budget._pending is not None) == failed
    assert budget.authoritative_charge_rounds == (0 if failed else 1)
    receipt = budget.finalize('failed' if failed else 'completed')
    verified = EvidenceStore(tmp_path / 'research' / 'request-budget-evidence').verify(receipt['attempt_id'])
    assert verified
    child = json.loads(seen[0].data)['requests'][0]['body']
    commitment = dict(evidence)['request_budget_commitment']
    assert commitment['request_sha256'] == hashlib.sha256(adapters.canonical(child).encode() + b'\n').hexdigest()
    assert child['stream'] is False and child['model'] == batch.BASE_MODEL


@pytest.mark.parametrize('case', ['http_error', 'redirect', 'oversized', 'bad_id'])
def test_submission_failure_never_polls_or_resubmits(monkeypatch, case):
    import urllib.error

    events, evidence, seen = [], [], []
    def send(request, **kwargs):
        seen.append(request)
        if case == 'http_error':
            raise urllib.error.HTTPError(request.full_url, 429, 'fixture', {}, io.BytesIO(b'bounded failure'))
        value = {'id': '../../escape' if case == 'bad_id' else 'batch_fixture',
                 'model': batch.BASE_MODEL, 'endpoint': '/v1/chat/completions', 'status': 'validating'}
        if case == 'oversized':
            value['padding'] = 'x' * batch.MAX_BYTES
        return Response('https://unapproved.invalid' if case == 'redirect' else request.full_url, value, 202)
    def opener(proxy, redirects):
        assert proxy.proxies == {} and isinstance(redirects, adapters._RejectRedirects)
        return SimpleNamespace(open=send)
    monkeypatch.setattr(batch.urllib.request, 'build_opener', opener)
    with gateway(events, evidence) as g:
        with pytest.raises(adapters.AdapterError):
            g.completion(payload())
        with pytest.raises(adapters.AdapterError):
            g.completion(payload())
        assert len(g.budget.committed) == 1 and g.budget.observed == []
    assert len(seen) == 1 and seen[0].method == 'POST'
    if case == 'http_error':
        assert dict(evidence)['opencode_provider_http_error']['status'] == 429


def test_batch_cannot_run_without_budget(monkeypatch):
    events, evidence = [], []
    seen = install(monkeypatch, events)
    with gateway(events, evidence) as g:
        g.budget = None
        with pytest.raises(adapters.AdapterError, match='requires request budget'):
            g.completion(payload())
    assert seen == []
