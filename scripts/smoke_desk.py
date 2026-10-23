"""Live synthetic desk examples; requires running server and configured LLM."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = {'synthetic': True, 'passed': False, 'checks': [], 'tasks': []}
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=100) as client:
        def request(method, path, **kwargs):
            response = client.request(method, '/api' + path, **kwargs)
            response.raise_for_status()
            return response.json()

        try:
            examples = request('GET', '/examples')
            selected = [next(e for e in examples if e['agent'] == agent) for agent in ('knowledge', 'analyst', 'reporter')]
            submitted = [(example, request('POST', '/examples/' + example['id'] + '/run', json={})) for example in selected]
            deadline = time.monotonic() + 240
            for example, task in submitted:
                while time.monotonic() < deadline:
                    task = request('GET', '/tasks/' + task['id'])
                    if task['status'] not in ('queued', 'running'):
                        break
                    time.sleep(1)
                assert task['status'] == 'completed', task.get('summary')
                result = task['result']
                assert result['generation'] == 'llm', result.get('generation_label')
                assert result['evidence_validation']['accepted_model_citations'] > 0
                assert result['sources'] and task['artifacts']
                for artifact in task['artifacts']:
                    response = client.get(artifact['url'])
                    response.raise_for_status()
                    assert response.content
                report['tasks'].append({'id': task['id'], 'agent': task['agent'], 'example_id': example['id'],
                                        'generation': result['generation'], 'sources': len(result['sources']),
                                        'evidence_validation': result['evidence_validation'],
                                        'artifacts': len(task['artifacts']), 'metrics': result.get('metrics')})
                report['checks'].append({'name': task['agent'] + ': live LLM + verified source excerpts + three downloadable exports', 'passed': True})
                if task['agent'] == 'analyst':
                    assert result['changes'] and result['actions']
                    payload = {'action_ids': [result['actions'][0]['id']]}
                    first = request('POST', '/tasks/' + task['id'] + '/actions/import', json=payload)
                    second = request('POST', '/tasks/' + task['id'] + '/actions/import', json=payload)
                    assert first['created'] == 1 and second['created'] == 0 and second['existing'] == 1
                    assert first['tasks'][0]['id'] == second['tasks'][0]['id']
                    assert first['tasks'][0]['synthetic'] and first['tasks'][0]['status'] == 'todo'
                    report['checks'].append({'name': 'selected follow-up becomes one persistent task; repeated import is idempotent', 'passed': True})
            for name in ('demo_v3_example_excel_changes', 'demo_v3_example_excel_reordered', 'demo_v3_example_excel_batch'):
                task = request('POST', '/examples/' + name + '/run', json={})
                for _ in range(30):
                    task = request('GET', '/tasks/' + task['id'])
                    if task['status'] not in ('queued', 'running'):
                        break
                    time.sleep(0.5)
                assert task['status'] == 'completed', task.get('summary')
                assert task['artifacts'] and task['synthetic']
                report['checks'].append({'name': name + ': actual inspection and artifacts', 'passed': True, 'task_id': task['id']})
            report['passed'] = True
        except Exception as exc:
            report['error'] = str(exc)[:700]
            raise
        finally:
            report['finished_at'] = datetime.now(timezone.utc).isoformat()
            destination = ROOT / 'verification' / 'v0.3' / 'live-desk.json'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
