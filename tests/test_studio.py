"""Offline tests for custom workflows, API boundaries, and local HTTP guards."""

from copy import deepcopy
import http.client
import json
import threading
import time
import types
import unittest
from unittest.mock import patch

from studio.runner import RunCancelled, call_agent, run_custom, safe_error, validate_connection, validate_workflow
from studio.server import StudioServer, redact


def workflow(mode='parallel', rounds=1):
    return {'name': 'Test workflow', 'task': 'Use this synthetic input only.', 'layers': [{'name': 'Evidence', 'mode': mode, 'rounds': rounds, 'agents': [{'role': 'First', 'model': 'test-model', 'prompt': 'Read the task.'}, {'role': 'Second', 'model': 'test-model', 'prompt': 'Check the task.'}]}, {'name': 'Review', 'mode': 'parallel', 'rounds': 1, 'agents': [{'role': 'Final', 'model': 'test-model', 'prompt': 'Review the earlier replies.'}]}], 'output': 'text', 'max_tokens': 128}


class WorkflowTests(unittest.TestCase):
    def test_parallel_roles_are_blind_to_same_layer(self):
        seen = {}
        def caller(agent, messages, connection, max_tokens, schema):
            seen[agent['role']] = deepcopy(messages)
            return agent['role'] + ' result'
        result = run_custom(validate_workflow(workflow()), {'mode': 'demo'}, lambda event: None, caller=caller)
        self.assertEqual(len(seen['First']), 2)
        self.assertEqual(len(seen['Second']), 2)
        self.assertEqual(len(seen['Final']), 4)
        self.assertIn('First result', seen['Final'][2]['content'])
        self.assertIn('Second result', seen['Final'][3]['content'])
        self.assertEqual(result['calls'], 3)
        self.assertFalse(result['paper_equivalent'])

    def test_debate_has_ordered_turns_and_rounds(self):
        lengths = []
        def caller(agent, messages, connection, max_tokens, schema):
            lengths.append((agent['role'], len(messages)))
            return 'Synthetic reply'
        result = run_custom(validate_workflow(workflow('debate', 2)), {'mode': 'demo'}, lambda event: None, caller=caller)
        self.assertEqual(lengths, [('First', 2), ('Second', 3), ('First', 4), ('Second', 5), ('Final', 6)])
        self.assertEqual(result['calls'], 5)
        self.assertEqual([row['round'] for row in result['trace']], [1, 1, 2, 2, 1])

    def test_sequential_mode_runs_once(self):
        config = validate_workflow(workflow('sequential', 5))
        self.assertEqual(config['layers'][0]['rounds'], 1)
        result = run_custom(config, {'mode': 'demo'}, lambda event: None)
        self.assertEqual(result['calls'], 3)
        self.assertTrue(all('Demo only' in row['output'] for row in result['trace']))

    def test_json_applies_to_last_layer_and_is_checked(self):
        config = workflow()
        config.update(output='json', schema={'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary'], 'additionalProperties': False})
        result = run_custom(validate_workflow(config), {'mode': 'demo'}, lambda event: None)
        self.assertIsInstance(result['trace'][0]['output'], str)
        self.assertEqual(result['outputs'][0]['output'], {'summary': 'Demo value'})
        with self.assertRaisesRegex(ValueError, 'does not match'):
            run_custom(validate_workflow(config), {'mode': 'demo'}, lambda event: None, caller=lambda *args: {'wrong': 'field'})

    def test_invalid_schema_and_call_count_are_rejected(self):
        config = workflow()
        config.update(output='json', schema={'type': 'object', 'properties': {'x': {'$ref': 'file:///private/file'}}})
        with self.assertRaisesRegex(ValueError, 'without reference'):
            validate_workflow(config)
        layer = {'name': 'Too many', 'mode': 'debate', 'rounds': 5, 'agents': workflow()['layers'][0]['agents'] * 3}
        config = workflow()
        config['layers'] = [deepcopy(layer) for _ in range(3)]
        with self.assertRaisesRegex(ValueError, '80 calls'):
            validate_workflow(config)

    def test_validation_strips_unrecognised_fields(self):
        config = workflow()
        config['api_key'] = 'secret'
        config['layers'][0]['agents'][0]['api_key'] = 'secret'
        config['layers'][0]['agents'][0]['prompt'] = '<script>example input</script>'
        clean = validate_workflow(config)
        self.assertNotIn('secret', json.dumps(clean))
        self.assertEqual(clean['layers'][0]['agents'][0]['prompt'], '<script>example input</script>')

    def test_cancellation_prevents_new_calls(self):
        seen = []
        def caller(agent, *args):
            seen.append(agent['role'])
            return 'Synthetic reply'
        with self.assertRaises(RunCancelled):
            run_custom(validate_workflow(workflow('sequential')), {'mode': 'demo'}, lambda event: None, cancelled=lambda: bool(seen), caller=caller)
        self.assertEqual(seen, ['First'])

    def test_api_endpoint_rules_and_demo_have_no_secret(self):
        self.assertEqual(validate_connection({'mode': 'demo', 'api_key': 'secret'}), {'mode': 'demo'})
        self.assertEqual(validate_connection({'mode': 'live', 'endpoint': 'http://localhost:11434/v1', 'api_key': ''})['api_key'], '')
        for endpoint in ['http://example.com/v1', 'https://user:secret@example.com/v1', 'https://example.com/v1?key=secret', 'file:///tmp/model']:
            with self.assertRaises(ValueError):
                validate_connection({'mode': 'live', 'endpoint': endpoint, 'api_key': 'secret'})

    def test_live_adapter_uses_header_key_and_no_retries(self):
        captured = {}
        class FakeClient:
            def __init__(self, **kwargs):
                captured['client'] = kwargs
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def create(self, **kwargs):
                captured['request'] = kwargs
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"summary":"synthetic"}'), finish_reason='stop')])
        agent = workflow()['layers'][0]['agents'][0]
        messages = [{'role': 'user', 'content': 'Synthetic task'}]
        with patch.dict('sys.modules', {'openai': types.SimpleNamespace(OpenAI=FakeClient)}):
            value = call_agent(agent, messages, {'mode': 'live', 'endpoint': 'https://example.test/v1', 'api_key': 'test-secret'}, 128, {'type': 'object'})
        self.assertEqual(value, {'summary': 'synthetic'})
        self.assertEqual(captured['client']['max_retries'], 0)
        self.assertEqual(captured['client']['api_key'], 'test-secret')
        self.assertNotIn('test-secret', json.dumps(captured['request']))
        self.assertEqual(captured['request']['response_format'], {'type': 'json_object'})

    def test_redaction_and_provider_error_do_not_expose_input(self):
        secret = 'test-secret'
        self.assertEqual(redact({'output': [secret, 'text ' + secret]}, secret), {'output': ['[hidden]', 'text [hidden]']})
        error = type('APIConnectionError', (Exception,), {})(secret)
        self.assertNotIn(secret, safe_error(error))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = StudioServer(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = {'X-Studio-Token': self.server.token}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        client.request('GET' if body is None else 'POST', path, body=None if body is None else json.dumps(body), headers=request_headers)
        response = client.getresponse()
        status, payload, response_headers = response.status, response.read(), dict(response.getheaders())
        client.close()
        return status, json.loads(payload) if 'application/json' in response_headers.get('Content-Type', '') else payload, response_headers

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, job, _ = self.request('/api/jobs/' + job_id)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(0.01)
        self.fail('The test job did not finish.')

    def test_page_is_local_and_does_not_cache(self):
        status, body, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_host_origin_and_token_guards(self):
        for headers in [{'Host': 'attacker.test'}, {'Origin': 'https://attacker.test'}, {'X-Studio-Token': ''}, {'Sec-Fetch-Site': 'cross-site'}]:
            status, _, _ = self.request('/api/jobs/missing', headers=headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.request('/../../README.md')[0], 404)

    def test_http_demo_completes_without_api_or_disk_write(self):
        with patch('studio.runner.call_agent', wraps=call_agent) as caller:
            status, data, _ = self.request('/api/run', {'config': workflow(), 'connection': {'mode': 'demo', 'api_key': 'discard-this'}})
            self.assertEqual(status, 202)
            job = self.wait_job(data['id'])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['result']['mode'], 'demo')
        self.assertEqual(caller.call_count, 3)
        self.assertNotIn('discard-this', json.dumps(job))

    def test_http_rejects_invalid_workflow_before_job(self):
        config = workflow()
        config['layers'] = []
        status, data, _ = self.request('/api/run', {'config': config, 'connection': {'mode': 'demo'}})
        self.assertEqual(status, 400)
        self.assertIn('1–6 layers', data['error'])

    def test_paper_cancel_survives_evidence_failure_wrapper(self):
        entered, release = threading.Event(), threading.Event()
        def fake_paper(config, connection, progress):
            entered.set()
            release.wait(3)
            from finder_core.workflow import EvidenceBranchFailure
            try:
                progress({'kind': 'message', 'message': 'Next stage'})
            except RunCancelled as exc:
                raise EvidenceBranchFailure('A branch stopped.') from exc
            self.fail('Cancellation should stop this stage.')
        with patch('studio.paper.run_paper', fake_paper):
            status, data, _ = self.request('/api/paper-run', {'config': {}, 'connection': {'mode': 'live', 'endpoint': 'https://example.test/v1', 'api_key': 'test-secret'}})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request('/api/cancel', {'id': data['id']})[0], 200)
            release.set()
            job = self.wait_job(data['id'])
        self.assertEqual(job['status'], 'cancelled')
        self.assertNotIn('test-secret', json.dumps(job))

    def test_duplicate_active_run_is_rejected(self):
        entered, release = threading.Event(), threading.Event()
        def fake_custom(*args):
            entered.set()
            release.wait(3)
            return {'mode': 'demo'}
        with patch('studio.server.run_custom', fake_custom):
            status, first, _ = self.request('/api/run', {'config': workflow(), 'connection': {'mode': 'demo'}})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            status, data, _ = self.request('/api/run', {'config': workflow(), 'connection': {'mode': 'demo'}})
            self.assertEqual(status, 400)
            self.assertIn('already active', data['error'])
            release.set()
            self.assertEqual(self.wait_job(first['id'])['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
