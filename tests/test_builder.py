import json
import os
from pathlib import Path
import tempfile
import unittest

from backend.builder import create_app, develop_app, _apply_requirement


class BuilderTests(unittest.TestCase):
    def test_generation_escapes_names_and_limits_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            result = create_app('견적 검수 앱', 'app-test', Path(folder), {
                'name': '</script><script>bad()</script>',
                'schema': {'fields': [{'name': 'budget', 'label': '예산', 'type': 'number', 'required': True, 'min': 0, 'max': 1000},
                                      {'name': '__proto__', 'type': 'text'}, {'name': 'id', 'type': 'text'},
                                      {'name': 'bad-name', 'type': 'text'}, {'name': 'script', 'type': 'javascript'},
                                      {'name': 'title', 'required': False}]}})
            source = (Path(folder) / result['source_filename']).read_text(encoding='utf-8')
            self.assertNotIn('</script><script>bad()', source)
            self.assertNotIn('innerHTML', source)
            self.assertEqual([f['name'] for f in result['schema']['fields']],
                             ['title', 'owner', 'due_date', 'status', 'completion_note', 'budget'])
            self.assertTrue(result['schema']['fields'][0]['required'])
            self.assertEqual(result['schema']['fields'][-1]['max'], 1000)
            self.assertTrue(result['warnings'])
            self.assertIn('/api/apps/', source)

    def test_malformed_schema_and_custom_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            result = create_app('업무 관리', 'test', Path(folder), {'fields': None})
            self.assertEqual(len(result['schema']['fields']), 5)
            self.assertTrue(result['warnings'])
            fields = [{'name': 'custom_'+str(i), 'label': '입력'*30, 'type': 'text'} for i in range(12)]
            result = create_app('업무 관리', 'test', Path(folder), {'fields': fields})
            self.assertEqual(len(result['schema']['fields']), 13)
            self.assertLessEqual(len(result['schema']['fields'][5]['label']), 40)

    def test_partial_change_preserves_constraints_and_rejects_malformed_edits(self):
        initial, _, _ = _apply_requirement(None, '', {'fields': [
            {'name': 'budget', 'type': 'number', 'required': True, 'min': 0, 'max': 1000},
            {'name': 'category', 'type': 'select', 'options': ['내부', '고객']}]})
        changed, changes, warnings = _apply_requirement(initial, '', {'fields': [
            {'name': 'budget', 'required': False, 'max': 2000}]})
        budget = next(f for f in changed['fields'] if f['name'] == 'budget')
        self.assertFalse(budget['required'])
        self.assertEqual(budget['min'], 0)
        self.assertEqual(budget['max'], 2000)
        self.assertTrue(changes)
        self.assertEqual(len(changed['fields']), 7)
        invalid, _, warnings = _apply_requirement(changed, '', {'fields': [
            {'name': 'budget', 'required': 'yes', 'min': 'unsafe'},
            {'name': 'category', 'options': {'invalid': True}}]})
        self.assertEqual(invalid, changed)
        self.assertTrue(warnings)

    def test_structured_plan_does_not_infer_unrelated_required_base_fields(self):
        prompt = ('기본 업무명·담당자·기한·상태·완료 메모 외에 '
                  'customer(고객명,text,필수), amount(금액,number,필수), '
                  'requester_email(이메일,email,선택) 세 필드 추가')
        with tempfile.TemporaryDirectory() as folder:
            created = create_app(prompt, 'app-test', Path(folder), {'schema': {'fields': [
                {'name': 'customer', 'label': '고객명', 'type': 'text', 'required': True},
                {'name': 'amount', 'label': '금액', 'type': 'number', 'required': True, 'min': 0},
                {'name': 'requester_email', 'type': 'email', 'required': False}]}})
            schema = created['schema']
            self.assertFalse(schema['require_completion_note'])
            fields = {f['name']: f for f in schema['fields']}
            self.assertFalse(fields['owner']['required'])
            self.assertFalse(fields['due_date']['required'])
            self.assertTrue(fields['customer']['required'])
            self.assertTrue(fields['amount']['required'])
            changed, changes, _ = _apply_requirement(schema, '완료 메모와 담당자, 기한 필수', {
                'require_completion_note': True,
                'fields': [{'name': 'owner', 'required': True}, {'name': 'due_date', 'required': True}]})
            self.assertTrue(changed['require_completion_note'])
            self.assertTrue(changes)
            self.assertTrue(all(f['required'] for f in changed['fields'] if f['name'] in ('owner', 'due_date')))

    def test_only_absent_plan_uses_demo_keyword_fallback(self):
        requirement = '완료 메모와 담당자, 마감일 필수. 과거 마감일 금지.'
        for plan in ({}, {'schema': {}}, {'name': '업무 앱'}):
            with self.subTest(plan=plan):
                schema, changes, _ = _apply_requirement(None, requirement, plan)
                self.assertFalse(schema['require_completion_note'])
                self.assertFalse(schema['prevent_past_due_date'])
                self.assertFalse(any(f['required'] for f in schema['fields'] if f['name'] in ('owner', 'due_date')))
                self.assertEqual(changes, [])
        with tempfile.TemporaryDirectory() as folder:
            schema = create_app(requirement, 'demo-app', Path(folder))['schema']
        self.assertTrue(schema['require_completion_note'])
        self.assertTrue(schema['prevent_past_due_date'])
        self.assertTrue(all(f['required'] for f in schema['fields'] if f['name'] in ('owner', 'due_date')))

    def test_unsupported_request_never_claims_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            created = create_app('업무 관리', 'app-test', root / 'created')
            app = {'id': 'app-test', **created, 'source_path': str(root / 'created' / 'app.html')}
            result = develop_app(app, '은행 시스템에 실제 송금 기능을 붙여줘', root / 'changed')
            self.assertFalse(result['passed'])
            self.assertEqual(result['tests'][0]['status'], 'blocked')
            self.assertIn('미승인', (root / 'changed' / 'uat-report.html').read_text(encoding='utf-8'))

    @unittest.skipUnless(os.environ.get('WORKMATE_BROWSER_TEST') == '1', 'Set WORKMATE_BROWSER_TEST=1 for real browser integration test')
    def test_supported_change_real_browser_and_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            created = create_app('업무 관리', 'app-test', root / 'created')
            app = {'id': 'app-test', **created, 'source_path': str(root / 'created' / 'app.html')}
            result = develop_app(app, '완료할 때 완료 메모를 필수로 하고 담당자와 마감일도 필수로 해줘. 과거 마감일 금지.', root / 'changed', plan={
                'require_completion_note': True,
                'prevent_past_due_date': True,
                'fields': [
                    {'name': 'owner', 'required': True},
                    {'name': 'due_date', 'required': True},
                    {'name': 'budget', 'label': '예산', 'type': 'number', 'required': True, 'min': 0, 'max': 1000},
                    {'name': 'contact_email', 'label': '회신 이메일', 'type': 'email', 'required': True},
                    {'name': 'category', 'label': '구분', 'type': 'select', 'required': True, 'options': ['내부', '고객']},
                    {'name': 'verified', 'label': '확인 여부', 'type': 'checkbox', 'required': True},
                    {'name': 'notes', 'label': '추가 메모', 'type': 'textarea'},
                    {'name': 'request_date', 'label': '접수일', 'type': 'date'},
                    {'name': 'project_code', 'label': '프로젝트 코드', 'type': 'text', 'required': True},
                    {'name': 'zero_count', 'label': '초기 건수', 'type': 'number', 'required': True, 'min': 0, 'max': 0}]})
            self.assertTrue(result['passed'], result['tests'])
            self.assertTrue(result['schema']['require_completion_note'])
            self.assertGreaterEqual(len(list((root / 'changed').glob('evidence-*.png'))), 5)
            self.assertTrue(all(t['status'] == 'passed' for t in result['tests']))
            report = json.loads((root / 'changed' / 'browser-results.json').read_text(encoding='utf-8'))
            self.assertEqual(report['source_sha256'], result['source_sha256'])
            self.assertIn('stateful mock', report['api_mode'])
            self.assertTrue((root / 'changed' / 'evidence-export.csv').exists())
            self.assertIn('TC-012', [t['id'] for t in result['tests']])


if __name__ == '__main__':
    unittest.main()
