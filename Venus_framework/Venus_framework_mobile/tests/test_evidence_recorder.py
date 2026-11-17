"""Offline evidence tests: no ADB, remote model, or fabricated real recordings."""
import base64
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from PIL import Image
from benchmarks.episode_evidence import EvidenceRecorder
from benchmarks.recorded_episode import run_recorded_episode


def image_data():
    stream = io.BytesIO()
    Image.new('RGB', (4, 4)).save(stream, format='PNG')
    return stream.getvalue()


def native_handler(directory):
    from app.run_handler import RunHandler
    with patch('app.run_handler.DeviceManager') as manager:
        manager.return_value.connect_device.return_value = True
        device = manager.return_value.get_device.return_value
        device.screenshot.return_value = base64.b64encode(image_data()).decode()
        handler = RunHandler('fixture', directory, policy_type='ui_venus_2',
                             ep_config={'step_limit': 5}, model_url='http://fixture/v1', model_name='fixture')
    handler.policy.get_next_action = Mock(return_value=(
        {'action_type': 'SUCCESS', 'input': '完成'}, 'Answer', 'Answer', '', 'Answer'))
    return handler, device


class EvidenceTests(unittest.TestCase):
    def test_api_images_are_saved_once_and_credentials_never_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(directory, 'fixture', {}, ['secret-for-fixture'])
            image = 'data:image/png;base64,' + base64.b64encode(image_data()).decode()
            index = recorder.model_request({'api_key': 'secret-for-fixture',
                'messages': [{'role': 'user', 'content': [
                    {'type': 'image_url', 'image_url': {'url': image}},
                    {'type': 'image_url', 'image_url': {'url': image}}]}]})
            recorder.model_response(index, {'choices': [{'message': {'content': 'secret-for-fixture'},
                                                        'finish_reason': 'stop'}]})
            files = list(Path(directory).rglob('*.json'))
            exported = '\n'.join(p.read_text() for p in files)
            self.assertNotIn('secret-for-fixture', exported)
            self.assertNotIn('data:image/', exported)
            images = list((Path(directory) / 'images').glob('*.png'))
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].read_bytes(), image_data())

    def test_failed_call_and_incomplete_video_are_explicit_and_log_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = EvidenceRecorder(directory, 'fixture', {}, ['secret-for-fixture'])
            def failed_video():
                recorder.video_error = 'FixtureRecorderFailure'
                recorder.ready.set()
            policy = Mock(client=None)
            with patch.object(recorder, 'screenshot'), patch.object(recorder, '_video', failed_video):
                with self.assertRaises(RuntimeError):
                    recorder.start(policy)
                # Send a WARNING even when the test root logger has its default level.
                logging.getLogger('evidence-fixture').warning('credential: secret-for-fixture')
                n = recorder.model_request({'messages': []})
                recorder.model_response(n, error=TimeoutError())
                recorder.close((False, 'recording_failed', None), {'final_answer': '', 'episode_data': []})
            root = Path(directory)
            summary = json.loads((root / 'summary.json').read_text())
            self.assertFalse(summary['evidence_complete'])
            self.assertEqual(summary['recording_error'], 'FixtureRecorderFailure')
            self.assertTrue((root / 'api-001-error.json').exists())
            self.assertTrue((root / 'trajectory.json').exists())
            self.assertNotIn('secret-for-fixture', (root / 'framework.log').read_text())
            self.assertIn('[REDACTED]', (root / 'framework.log').read_text())
            self.assertNotIn(recorder.handler, logging.getLogger().handlers)

    def test_external_wrapper_finalizes_and_restores_on_normal_exit_and_interrupt(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                handler, device = native_handler(directory)
                original_screen = handler.req_from_client
                recording = Mock()
                recording.video_error = None
                if interrupted:
                    handler.policy.get_next_action = Mock(side_effect=KeyboardInterrupt())
                with patch('benchmarks.recorded_episode.EvidenceRecorder', return_value=recording):
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt):
                            run_recorded_episode(handler, 'fixture interrupted', directory)
                    else:
                        self.assertTrue(run_recorded_episode(handler, 'fixture normal', directory)[0])
                recording.start.assert_called_once()
                recording.close.assert_called_once()
                args = recording.close.call_args.args
                self.assertEqual(args[1]['termination_reason'],
                                 'interrupted' if interrupted else 'success')
                if interrupted:
                    self.assertTrue(args[1]['partial'])
                    self.assertTrue(Path(args[1]['original_trajectory_file']).exists())
                self.assertEqual(handler.req_from_client, original_screen)
                device.tap.assert_not_called()

    def test_direct_handler_never_starts_external_recorder(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, _ = native_handler(directory)
            with patch('benchmarks.recorded_episode.EvidenceRecorder') as recorder:
                self.assertTrue(handler.run('fixture')[0])
                recorder.assert_not_called()

    def test_bad_trajectory_cannot_skip_recording_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = Mock(trace_dir=directory, device_id='fixture', final_answer='')
            def run(purpose):
                episode = Path(directory) / 'episode'
                episode.mkdir()
                (episode / 'trajectory.pkl.gz').write_bytes(b'corrupt')
                return (True, 'success', None)
            handler.run.side_effect = run
            recording = Mock(video_error=None)
            original_screen = handler.req_from_client
            with patch('benchmarks.recorded_episode.EvidenceRecorder', return_value=recording):
                self.assertTrue(run_recorded_episode(handler, 'fixture', directory)[0])
            recording.close.assert_called_once()
            self.assertIn('export_error', recording.close.call_args.args[1])
            self.assertEqual(handler.req_from_client, original_screen)


if __name__ == '__main__':
    unittest.main()
