from fastapi.testclient import TestClient
from backend import cloud
from backend.llm import Planner


def test_visitor_data_and_keys_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud,'SESSIONS',tmp_path/'sessions')
    monkeypatch.setenv('WORKMATE_LLM_API_KEY','owner-key-never-inherit')
    with TestClient(cloud.app) as a, TestClient(cloud.app) as b:
        assert a.get('/api/state').status_code==401
        assert a.get('/').status_code==200 and b.get('/').status_code==200
        assert a.cookies.get('workmate_session')!=b.cookies.get('workmate_session')
        assert not a.get('/api/state').json()['llm_configured']
        assert 'owner-key-never-inherit' not in a.get('/api/state').text
        r=a.post('/api/memories',json={'key':'private-note','value':'only visitor A'})
        assert r.status_code==200,r.text
        assert 'only visitor A' in a.get('/api/state').text
        assert 'only visitor A' not in b.get('/api/state').text
        monkeypatch.setattr(Planner,'test_connection',lambda self:self.public_status())
        r=a.post('/api/connection/config',json={'base_url':'https://api.openai.com/v1','model':'model','api_key':'visitor-A-key'})
        assert r.status_code==200,r.text
        assert a.get('/api/connection/config').json()['has_key']
        assert not b.get('/api/connection/config').json()['has_key']
        assert not list(tmp_path.rglob('.env'))
        assert b.post('/api/connection/config',json={'base_url':'https://api.openai.com/v1','model':'x','api_key':'x','persist':True}).status_code==400
        assert a.post('/api/chat',json={'message':'hi'},headers={'Origin':'https://evil.example'}).status_code==403
        assert a.get('/api/state',headers={'Host':'evil.example'}).status_code==400
