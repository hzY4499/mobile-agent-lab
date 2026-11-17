"""Offline request boundary/deadline/finalization checks. No phone or model."""
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from benchmarks.episode_evidence import EvidenceRecorder
from benchmarks.recorded_episode import run_recorded_episode
from benchmarks.request_budget import RequestBudget, RequestBudgetStop


class BudgetTests(unittest.TestCase):
    def test_failed_requests_and_retries_reserve_before_dispatch_and_restore_sdk(self):
        try:
            import httpx2 as httpx
        except ImportError:
            import httpx
        from openai import OpenAI, APIStatusError
        received = []
        def backend(request):
            received.append(json.loads(request.content))
            return httpx.Response(500, json={'error': {'message': 'fixture'}})
        with tempfile.TemporaryDirectory() as directory:
            client = OpenAI(api_key='fixture', base_url='http://fixture/v1', max_retries=2,
                            http_client=httpx.Client(transport=httpx.MockTransport(backend)))
            original = client.chat.completions.create
            budget = RequestBudget(directory, model_request_limit=2, expected_model='qwen',
                                   expected_base_url='http://fixture/v1')
            recorder = EvidenceRecorder(directory, 'fixture', {}, request_budget=budget)
            recorder.attach_client(SimpleNamespace(client=client))
            self.assertEqual(client.max_retries, 0)
            for _ in range(2):
                with self.assertRaises(APIStatusError):
                    client.chat.completions.create(model='qwen', messages=[])
            with self.assertRaises(RequestBudgetStop) as exc:
                client.chat.completions.create(model='qwen', messages=[])
            self.assertEqual(exc.exception.reason, 'model_request_limit')
            self.assertEqual(len(received), 2)
            progress = json.loads((Path(directory) / 'budget-progress.json').read_text())
            self.assertEqual(progress['modelRequests'], 2)
            self.assertFalse(progress['terminal'])
            for restore in reversed(recorder.restores):
                restore()
            self.assertEqual(client.max_retries, 2)
            self.assertEqual(client.chat.completions.create, original)
            client.close()

    def test_identity_and_expired_deadline_rejected_without_reservation(self):
        for expected, request, url, reason in [
            ('qwen', 'other', 'http://fixture/v1/', 'model_identity_mismatch'),
            ('qwen', 'qwen', 'http://wrong/v1', 'endpoint_mismatch')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                guard = RequestBudget(directory, expected_model=expected, expected_base_url='http://fixture/v1')
                with self.assertRaises(RequestBudgetStop) as exc:
                    guard.admit({'model': request}, SimpleNamespace(base_url=url))
                self.assertEqual(exc.exception.reason, reason)
                self.assertEqual(guard.requests, 0)
        with tempfile.TemporaryDirectory() as directory:
            guard = RequestBudget(directory, deadline_ms=int(time.time() * 1000) - 1)
            with self.assertRaises(RequestBudgetStop):
                guard.admit({'model': 'qwen'}, SimpleNamespace(base_url='http://fixture/v1'))
            self.assertEqual(guard.requests, 0)

    def test_response_identity_and_remaining_transport_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = RequestBudget(directory, expected_model='qwen', deadline_ms=time.time()*1000+1000)
            request = guard.admit({'model': 'qwen', 'timeout': 120.0},
                                  SimpleNamespace(base_url='http://fixture/v1'))
            self.assertLessEqual(request['timeout'], 1.0)
            with self.assertRaises(RequestBudgetStop) as exc:
                guard.response(SimpleNamespace(model='other'))
            self.assertEqual(exc.exception.reason, 'response_model_identity_mismatch')

    def test_deadline_interrupt_finalizes_once_and_masks_repeat_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = RequestBudget(directory, deadline_ms=time.time()*1000+60)
            handler = SimpleNamespace(device_id='fixture', trace_dir=directory, policy=SimpleNamespace(client=None),
                                      req_from_client=lambda: {}, run=lambda purpose: time.sleep(2), final_answer='')
            recording = Mock(video_error=None)
            def close(*_):
                self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
            recording.close.side_effect = close
            previous_term, previous_alarm = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGALRM)
            with patch('benchmarks.recorded_episode.EvidenceRecorder', return_value=recording):
                result = run_recorded_episode(handler, 'fixture', directory, request_budget=guard)
            self.assertEqual(result[1], 'deadline_exceeded')
            recording.close.assert_called_once()
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous_term)
            self.assertEqual(signal.getsignal(signal.SIGALRM), previous_alarm)
            progress = json.loads(guard.path.read_text())
            self.assertTrue(progress['terminal'])
            self.assertEqual(progress['reason'], 'deadline_exceeded')

    def test_actual_device_dispatch_and_nested_double_tap_count(self):
        class Device:
            def tap(self, x, y): return True
            def double_tap(self, x, y):
                self.tap(x, y)
                return self.tap(x, y)
        with tempfile.TemporaryDirectory() as directory:
            device = Device()
            handler = SimpleNamespace(device_manager=SimpleNamespace(get_device=lambda _: device),
                                      device_id='fixture', runtime_context=SimpleNamespace(step=2))
            recorder = EvidenceRecorder(directory, 'fixture', {})
            original = device.tap
            recorder.attach_device(handler)
            self.assertTrue(device.double_tap(10, 20))
            self.assertTrue(device.tap(30, 40))
            events = [json.loads(line) for line in (Path(directory) / 'executions.jsonl').read_text().splitlines()]
            self.assertEqual(len(events), 2)
            self.assertEqual(sum(e['guiActionCount'] for e in events), 3)
            self.assertEqual(events[0]['action']['action_type'], 'DoubleCLK')
            self.assertTrue(all(e['success'] for e in events))
            for restore in reversed(recorder.restores): restore()
            self.assertEqual(device.tap, original)

    def test_partial_native_trajectory_kept_when_budget_interrupts(self):
        import gzip
        import pickle
        with tempfile.TemporaryDirectory() as directory:
            guard = RequestBudget(directory, model_request_limit=1)
            handler = SimpleNamespace(device_id='fixture', trace_dir=directory, policy=SimpleNamespace(client=None),
                                      req_from_client=lambda: {}, final_answer='', runtime_context=SimpleNamespace(step=2))
            def run(_):
                active = Path(directory) / 'native-episode'
                active.mkdir()
                handler._active_episode_dir = str(active)
                handler._partial_episode_data = [{'step': 1, 'action': {'action_type': 'CLK'}, 'success': True}]
                guard.stop('model_request_limit')
            handler.run = run
            recording = Mock(video_error=None)
            with patch('benchmarks.recorded_episode.EvidenceRecorder', return_value=recording):
                result = run_recorded_episode(handler, 'fixture', directory, request_budget=guard)
            self.assertEqual(result[1], 'model_request_limit')
            trajectory = recording.close.call_args.args[1]
            self.assertTrue(trajectory['partial'])
            self.assertEqual(len(trajectory['episode_data']), 1)
            with gzip.open(trajectory['original_trajectory_file'], 'rb') as file:
                self.assertEqual(pickle.load(file)['episode_data'][0]['step'], 1)

    def test_constructor_failure_publishes_terminal_zero_without_fake_recording(self):
        from scripts import run_recorded_venus
        with tempfile.TemporaryDirectory() as directory:
            config = {'device': {'id': 'fixture'}, 'ep_config': {},
                      'policy': {'type': 'ui_venus_2', 'params': {}}}
            with patch.object(sys, 'argv', ['recorded', '--purpose', 'fixture', '--evidence-dir', directory,
                                           '--model-request-limit', '1']), \
                 patch.object(run_recorded_venus, 'load_config', return_value=config), \
                 patch.object(run_recorded_venus, 'setup_logging'), \
                 patch.object(run_recorded_venus, 'RunHandler', side_effect=ValueError('fixture startup')), \
                 patch.object(run_recorded_venus.logging, 'exception'):
                self.assertEqual(run_recorded_venus.main(), 1)
            progress = json.loads((Path(directory) / 'budget-progress.json').read_text())
            self.assertTrue(progress['terminal'])
            self.assertEqual(progress['modelRequests'], 0)
            self.assertEqual(progress['reason'], 'startup_failed')
            self.assertFalse((Path(directory) / 'summary.json').exists())


if __name__ == '__main__': unittest.main()
