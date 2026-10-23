"""Actual LLM -> custom app -> UAT -> Excel workflow registration, synthetic data."""
import json
import time
from datetime import datetime
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
report = {'synthetic_data': True, 'passed': False, 'tests': []}


def main():
    with httpx.Client(base_url='http://127.0.0.1:8765/api', timeout=100) as c:
        def post(path, data):
            r = c.post(path, json=data)
            r.raise_for_status()
            return r.json()
        def wait(task):
            for _ in range(180):
                result = c.get('/tasks/' + task['id']).json()
                if result['status'] not in ('queued', 'running'):
                    return result
                time.sleep(1)
            raise AssertionError('Task timeout')
        try:
            built = wait(post('/apps', {'prompt': '[검증] 고객 요청 대장을 만들어줘. 기본 업무명·담당자·기한·상태·완료 메모 외에 customer(고객명,text,필수), amount(예상 금액,number,최소 0,필수), requester_email(요청자 이메일,email,선택) 세 필드만 추가해줘.'}))
            assert built['status'] == 'completed', built['summary']
            app_id = built['result']['app_id']
            report['app_id'] = app_id
            report['builder_task_id'] = built['id']
            fields = {f['name']: f for f in built['result']['schema']['fields']}
            assert len(fields) == 8 and fields['customer']['required'] and fields['amount']['min'] == 0
            report['tests'].append({'name': '실제 LLM이 요청한 사용자 정의 3필드를 생성', 'passed': True})
            developed = wait(post('/tasks', {'agent': 'developer', 'title': '[검증] 고객 요청 완료 규칙 검증', 'input': {'app_id': app_id, 'requirement': '완료 상태에서는 완료 메모를 반드시 입력하도록 변경하고 나머지 필드는 유지해줘.'}}))
            assert developed['status'] == 'completed', developed['summary']
            assert developed['result']['passed'] and developed['result']['applied']
            report['developer_task_id'] = developed['id']
            report['uat_scenarios'] = len(developed['result']['tests'])
            report['tests'].append({'name': '실제 LLM 요구사항 해석 → 코드 변경 → 브라우저 UAT → 앱 반영', 'passed': True})
            workflow = post('/workflows', {'name': '[검증] 고객 요청 검수 등록', 'app_id': app_id, 'current': 'clean'})
            task = wait(post('/workflows/' + workflow['id'] + '/run', {}))
            assert task['status'] == 'waiting_input' and task['result']['checkpoint'] == 'registration'
            assert task['artifacts']
            report['tests'].append({'name': 'Excel 검수 증빙을 보존하고 필수 입력 대기', 'passed': True})
            resumed = wait(post('/tasks/' + task['id'] + '/resume', {'action': 'continue', 'input': {'record_values': {'customer': '고객사 A', 'amount': 0, 'requester_email': 'qa@example.test'}}}))
            assert resumed['status'] == 'completed', resumed['summary']
            records = c.get('/apps/' + app_id + '/records').json()
            assert len(records) == 1 and records[0]['customer'] == '고객사 A' and records[0]['amount'] == 0
            report['workflow_task_id'] = task['id']
            report['tests'].append({'name': '추가 입력 후 실제 업무 앱 DB에 정확히 1건 등록', 'passed': True})
            report['passed'] = True
        except Exception as exc:
            report['error'] = str(exc)[:600]
            raise
        finally:
            report['finished_at'] = datetime.now().isoformat()
            output = ROOT / 'data' / 'artifacts' / 'dynamic-live-results.json'
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
