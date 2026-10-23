"""Bounded live model smoke through the local API. Uses synthetic text only.

API credentials stay on the backend. This script makes four paid model requests.
"""
import json
from pathlib import Path
from datetime import datetime
import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'data' / 'artifacts' / 'live-llm-results.json'


def main():
    results = {'synthetic_data': True, 'started_at': datetime.now().isoformat(), 'tests': [], 'passed': False}
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=100) as client:
        def call(path, payload=None):
            response = client.post('/api' + path, json=payload or {})
            response.raise_for_status()
            return response.json()
        try:
            status = call('/connection/test')
            assert status['status'] == 'connected'
            results['model'] = status['model']
            results['tests'].append({'name': '실제 모델 연결', 'passed': True})
            answer = call('/chat', {'message': '기억해줘. 항목 이름은 [검증] 보고 선호이고, 내용은 업무 보고를 세 문장으로 작성 이라고 저장해줘.'})
            assert answer.get('memory') and '세 문장' in answer['memory']['value']
            results['tests'].append({'name': '자연어 선호 기억 저장', 'passed': True})
            answer = call('/chat', {'message': '개인 할 일로 [검증] 제안서 확인을 등록해줘. 기한은 지금부터 30분 후야.'})
            tid = answer['task_id']
            task = client.get('/api/tasks/' + tid).json()
            assert task['input'].get('manual') and task['due_at'] and '제안서' in task['title']
            results['tests'].append({'name': '자연어 업무·상대 기한 등록', 'passed': True, 'task_id': tid, 'due_at': task['due_at']})
            answer = call('/chat', {'message': '방금 등록한 제안서 확인 업무의 제목과 기한을 알려줘.'})
            assert '제안서' in answer['reply'] and not answer.get('task_id')
            results['tests'].append({'name': '이전 대화·실제 업무 기록 참조', 'passed': True})
            history = client.get('/api/chat/history').json()
            assert len(history) >= 6
            results['tests'].append({'name': '대화 기록 저장', 'passed': True})
            # Keep test evidence available without showing a synthetic deadline as real work.
            client.patch('/api/tasks/' + tid, json={'archived': True}).raise_for_status()
            client.delete('/api/memories/' + next(m['id'] for m in client.get('/api/state').json()['memories'] if m['key'] == '[검증] 보고 선호')).raise_for_status()
            results['connection'] = client.get('/api/state').json()['llm']
            results['passed'] = True
        except Exception as exc:
            results['error'] = str(exc)[:600]
            raise
        finally:
            results['finished_at'] = datetime.now().isoformat()
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'passed': results['passed'], 'tests': len(results['tests']), 'model': results.get('model'), 'evidence': str(REPORT)}, ensure_ascii=True))


if __name__ == '__main__':
    main()
