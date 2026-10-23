import pytest
from backend.connection import ConnectionRequest, candidate, normalize_base, persist_settings
from backend.llm import Planner
from test_platform import client


def test_connection_never_returns_secret_and_rejects_key_reuse_to_new_host(client, monkeypatch):
    monkeypatch.setattr(Planner, 'test_connection', lambda self: self.public_status())
    key = 'private-connection-value'
    r = client.post('/api/connection/config',json={'base_url':'https://api.openai.com/v1/chat/completions','model':'model-id','api_key':key})
    assert r.status_code == 200, r.text
    assert key not in r.text and r.json()['has_key']
    assert r.json()['base_url']=='https://api.openai.com/v1'
    assert key not in client.get('/api/state').text
    r = client.post('/api/connection/config',json={'base_url':'https://different.example/v1','model':'model-id'})
    assert r.status_code == 400
    assert client.get('/api/connection/config').json()['base_url']=='https://api.openai.com/v1'


def test_failed_connection_does_not_replace_working_config(client, monkeypatch):
    monkeypatch.setattr(Planner, 'test_connection', lambda self: self.public_status())
    values={'base_url':'https://api.openai.com/v1','model':'before','api_key':'one'}
    assert client.post('/api/connection/config',json=values).status_code==200
    def fail(self): raise ValueError('HTTP 401 인증 확인')
    monkeypatch.setattr(Planner,'test_connection',fail)
    assert client.post('/api/connection/config',json={**values,'model':'after'}).status_code==400
    assert client.get('/api/connection/config').json()['model']=='before'


def test_validation_does_not_echo_bad_key(client):
    key='do-not-echo-'*900
    r=client.post('/api/connection/config',json={'base_url':'https://api.openai.com/v1','model':'model','api_key':key})
    assert r.status_code==422 and 'do-not-echo-' not in r.text


@pytest.mark.parametrize('url',['http://example.com/v1','https://user:password@example.com/v1','https://example.com/v1?key=secret','file:///tmp/key','https://example.com/#frag'])
def test_endpoint_restrictions(url):
    with pytest.raises(ValueError): normalize_base(url)


def test_hosted_rejects_internal_and_custom_proxy():
    for url in ['http://127.0.0.1:11434/v1','https://127.0.0.1','https://internal.example','https://api.openai.com:444/v1']:
        with pytest.raises(ValueError): normalize_base(url,True)
    assert normalize_base('https://api.openai.com/v1',True)=='https://api.openai.com/v1'


def test_settings_file_preserves_unrelated_settings(tmp_path):
    p=tmp_path/'.env';p.write_text('WORKMATE_CUSTOM=retained\nWORKMATE_LLM_API_KEY=old\n',encoding='utf-8')
    planner=Planner(inherit_env=False);planner.base='https://api.example/v1';planner.key='new-key';planner.model='model'
    persist_settings(p,planner)
    text=p.read_text();assert 'WORKMATE_CUSTOM=retained' in text and 'old' not in text
    assert text.count('WORKMATE_LLM_API_KEY=')==1
