"""Offline checks for per-step timing. No phone, no model."""
import base64
import gzip
import io
import json
import pickle
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

FW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FW))
from utils import step_timing


def png_bytes(size=(32, 48)):
    buffer = io.BytesIO()
    Image.new('RGB', size, 'white').save(buffer, 'PNG')
    return buffer.getvalue()


class SummaryTests(unittest.TestCase):
    def test_summary_statistics_only_cover_steps_that_have_the_segment(self):
        timings = [
            {'step': 1, 'step_total_s': 4.0, 'model_s': 1.0, 'wait_s': 2.5, 'model_attempts': 1},
            {'step': 2, 'step_total_s': 8.0, 'model_s': 3.0, 'wait_s': 2.5, 'model_attempts': 3},
            {'step': 3, 'step_total_s': 6.0, 'model_s': 2.0, 'model_attempts': 1},
        ]
        summary = step_timing.summarize(timings)
        self.assertEqual(summary['steps'], 3)
        self.assertEqual(summary['segments']['step_total_s'],
                         {'n': 3, 'mean': 6.0, 'median': 6.0, 'max': 8.0, 'total': 18.0})
        self.assertEqual(summary['segments']['model_s']['median'], 2.0)
        self.assertEqual(summary['segments']['wait_s']['n'], 2)
        self.assertNotIn('action_s', summary['segments'])
        self.assertEqual(summary['counts']['model_attempts']['max'], 3)
        self.assertEqual(summary['counts']['model_attempts']['total'], 5)

    def test_empty_summary(self):
        self.assertEqual(step_timing.summarize([]), {'steps': 0, 'segments': {}, 'counts': {}})

    def test_png_header_size_and_log_round_trip(self):
        self.assertEqual(step_timing.png_size(png_bytes((32, 48))), (32, 48))
        self.assertIsNone(step_timing.png_size(b'not a png'))
        line = '2026-01-01 - app.run_handler - INFO - ' + step_timing.format_step_log(
            {'step': 2, 'step_total_s': 1.23456789, 'model_s': 0.5})
        self.assertNotIn('\n', line)
        parsed = step_timing.timings_from_log(['unrelated 当前步数: 2', line, 'STEP_TIMING {broken'])
        self.assertEqual(parsed, [{'step': 2, 'step_total_s': 1.2346, 'model_s': 0.5}])

    def test_call_recorder_counts_attempts_and_http_requests(self):
        recorder = step_timing.CallRecorder()
        with recorder.track() as call:
            call.attempts += 1
            recorder.note_http_request()
            recorder.note_http_request()
        recorder.note_http_request()
        with self.assertRaises(ValueError):
            with recorder.track() as call:
                call.attempts += 1
                raise ValueError
        snapshot = recorder.snapshot()
        self.assertEqual((snapshot['calls'], snapshot['attempts'], snapshot['http_requests']), (2, 2, 2))
        recorder.reset()
        self.assertEqual(recorder.snapshot()['calls'], 0)

    def test_cli_prints_stats_from_trajectory_directory(self):
        trajectory = {'episode_data': [
            {'step': 1, 'timing': {'step': 1, 'step_total_s': 5.0, 'model_s': 2.0}},
            {'step': 2, 'step_time': 7.0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            with gzip.open(Path(directory) / 'trajectory.pkl.gz', 'wb') as file:
                pickle.dump(trajectory, file)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(step_timing.main([directory]), 0)
            self.assertIn('step_total_s', out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(step_timing.main([directory, '--json']), 0)
            self.assertEqual(json.loads(out.getvalue())['summary']['segments']['step_total_s']['max'], 7.0)


class DeviceTests(unittest.TestCase):
    def test_screenshot_records_capture_encode_size_and_retries(self):
        from device import adb_controller
        image = png_bytes((32, 48))
        results = iter([SimpleNamespace(returncode=1, stdout=b''), SimpleNamespace(returncode=0, stdout=image)])
        with patch.object(adb_controller.ADBController, '_check_connection'), \
                patch.object(adb_controller.subprocess, 'run', side_effect=lambda *a, **k: next(results)), \
                patch.object(adb_controller.time, 'sleep'):
            controller = adb_controller.ADBController('fixture')
            encoded = controller.screenshot()
        self.assertEqual(base64.b64decode(encoded), image)
        stats = controller.last_screenshot_stats
        self.assertEqual((stats['attempts'], stats['bytes'], stats['width'], stats['height']), (2, len(image), 32, 48))
        self.assertGreaterEqual(stats['capture_s'], 0)

    def test_failed_screenshot_still_reports_attempts(self):
        from device import adb_controller
        with patch.object(adb_controller.ADBController, '_check_connection'), \
                patch.object(adb_controller.subprocess, 'run',
                             return_value=SimpleNamespace(returncode=1, stdout=b'')), \
                patch.object(adb_controller.time, 'sleep'):
            controller = adb_controller.ADBController('fixture')
            self.assertIsNone(controller.screenshot())
        self.assertEqual(controller.last_screenshot_stats['attempts'], 3)
        self.assertEqual(controller.last_screenshot_stats['bytes'], 0)


class SdkCounterTests(unittest.TestCase):
    def test_http_requests_counts_sdk_level_retries(self):
        import httpx
        from openai import OpenAI
        calls = []

        def backend(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(500, json={'error': {'message': 'boom'}})
            return httpx.Response(200, json={'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'm',
                                             'choices': [{'index': 0, 'finish_reason': 'stop',
                                                          'message': {'role': 'assistant', 'content': 'ok'}}]})
        client = OpenAI(api_key='k', base_url='http://fixture/v1', max_retries=2,
                        http_client=httpx.Client(transport=httpx.MockTransport(backend)))
        recorder = step_timing.CallRecorder()
        recorder.attach_http_counter(client)
        with patch('time.sleep'), recorder.track() as call:
            call.attempts += 1
            client.chat.completions.create(model='m', messages=[])
        snapshot = recorder.snapshot()
        self.assertEqual((snapshot['attempts'], snapshot['http_requests']), (1, 2))


class RunLoopTests(unittest.TestCase):
    def run_episode(self, reflection=False):
        import app.run_handler as run_handler
        image = png_bytes((32, 48))
        device = MagicMock()
        device.tap.return_value = True

        def screenshot():
            device.last_screenshot_stats = {'attempts': 1, 'capture_s': 0.25, 'encode_s': 0.01,
                                            'bytes': len(image), 'width': 32, 'height': 48}
            return base64.b64encode(image).decode()
        device.screenshot.side_effect = screenshot
        manager = MagicMock()
        manager.connect_device.return_value = True
        manager.get_device.return_value = device
        replies = iter(['<action>Click(point=(100, 200))</action>', '<action>Finished(content="done")</action>'])

        def create(**_):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))])
        ep_config = {'step_limit': 5}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(run_handler, 'DeviceManager', return_value=manager), \
                patch('policy.ui_venus_2_policy.OpenAI') as sdk, \
                patch.object(run_handler.time, 'sleep') as sleep:
            sdk.return_value.chat.completions.create.side_effect = create
            handler = run_handler.RunHandler('fixture', directory, 'ui_venus_2', ep_config,
                                             model_url='http://fixture/v1', model_name='m')
            if reflection:
                recorder = step_timing.CallRecorder()

                def judge(**_):
                    with recorder.track() as call:
                        call.attempts = 2
                    return {'verdict': 'CORRECT'}
                handler.reflection_supervisor = SimpleNamespace(
                    max_retries=3, call_recorder=recorder, judge_action=judge,
                    notify_step_committed=lambda: None)
            with self.assertLogs('app.run_handler', 'INFO') as logs:
                result = handler.run('task')
            self.assertEqual(result[1], 'success')
            self.assertEqual(sleep.call_count, 1)
            trajectory_file = next(Path(directory).glob('*/trajectory.pkl.gz'))
            with gzip.open(trajectory_file, 'rb') as file:
                return pickle.load(file), logs.output

    def test_each_step_records_segments_and_summary(self):
        trajectory, logs = self.run_episode()
        steps = trajectory['episode_data']
        self.assertEqual(len(steps), 2)
        first, last = steps[0]['timing'], steps[1]['timing']
        for name in ('step_total_s', 'screenshot_s', 'model_s', 'model_call_s', 'save_screenshot_s',
                     'action_s', 'wait_s', 'other_s'):
            self.assertGreaterEqual(first[name], 0, name)
        self.assertEqual((first['screenshot_width'], first['screenshot_height']), (32, 48))
        self.assertEqual(first['screenshot_capture_s'], 0.25)
        self.assertEqual((first['model_calls'], first['model_attempts']), (1, 1))
        self.assertNotIn('reflection_s', first)
        self.assertNotIn('action_s', last)
        self.assertLessEqual(first['model_call_s'], first['model_s'] + 1e-6)
        parts = sum(first[k] for k in ('screenshot_s', 'model_s', 'save_screenshot_s', 'action_s',
                                       'wait_s', 'other_s'))
        self.assertAlmostEqual(parts, first['step_total_s'], places=6)
        self.assertEqual(trajectory['timing_summary']['steps'], 2)
        self.assertIn('step_time', steps[0])
        timing_lines = [line for line in logs if 'STEP_TIMING ' in line]
        self.assertEqual(len(timing_lines), 2)
        self.assertEqual(json.loads(timing_lines[0].split('STEP_TIMING ', 1)[1])['step'], 1)
        self.assertTrue(any('EPISODE_TIMING_SUMMARY ' in line for line in logs))
        self.assertTrue(logs[-1].endswith('is_successful=True'))

    def test_reflection_segments_are_separate(self):
        trajectory, _ = self.run_episode(reflection=True)
        timing = trajectory['episode_data'][0]['timing']
        self.assertEqual(timing['reflection_rounds'], 1)
        self.assertEqual(timing['reflection_judge_calls'], 1)
        self.assertEqual(timing['reflection_judge_attempts'], 2)
        self.assertEqual(timing['reflection_retry_calls'], 0)
        self.assertIn('reflection_s', trajectory['timing_summary']['segments'])


if __name__ == '__main__':
    unittest.main()
