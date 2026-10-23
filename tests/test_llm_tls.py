import ssl
from backend.llm import Planner


def test_llm_uses_system_trust_without_disabling_verification(monkeypatch):
    import backend.llm as llm
    captured = {}
    class Response:
        status_code = 200
        def json(self):
            return {'choices': [{'message': {'content': '{"ok":true}'}}], 'usage': {'total_tokens': 3, 'cost': .0001}}
    class Client:
        def __init__(self, **kwargs): captured.update(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(llm.httpx, 'Client', Client)
    planner = Planner()
    planner.key, planner.base, planner.model = 'synthetic-test-key', 'https://mock.invalid/v1', 'mock'
    result = planner.test_connection()
    context = captured['verify']
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert result['status'] == 'connected' and result['calls'] == 1
    assert result['total_tokens'] == 3 and result['cost_usd'] == .0001
    assert 'synthetic-test-key' not in str(result)


def test_truncated_reasoning_is_explicit_and_billed_usage_remains_visible(monkeypatch):
    import pytest
    import backend.llm as llm
    captured = {}
    class Response:
        status_code = 200
        def json(self):
            return {'choices': [{'finish_reason': 'length', 'message': {'content': None}}], 'usage': {'total_tokens': 2400, 'cost': .002}}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            captured.update(kwargs['json'])
            return Response()
    monkeypatch.setattr(llm.httpx, 'Client', Client)
    p = Planner()
    p.key, p.base, p.model, p.reasoning_effort = 'synthetic', 'https://openrouter.ai/api/v1', 'mock', 'none'
    with pytest.raises(ValueError, match='응답 길이 한도'):
        p.test_connection()
    assert captured['reasoning'] == {'effort': 'none', 'exclude': True}
    status = p.public_status()
    assert status['status'] == 'error' and status['calls'] == 1 and status['total_tokens'] == 2400
