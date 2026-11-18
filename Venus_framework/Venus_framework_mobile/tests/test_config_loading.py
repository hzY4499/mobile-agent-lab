"""Shared CLI configuration loading without connecting a phone or model."""
from argparse import Namespace
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import load_config


class ConfigLoadingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.args = Namespace(
            config=str(self.root / 'episode.yaml'), device_id=None, step_limit=None,
            model_url=None, model_name=None, reflection=False,
            reflection_config=str(self.root / 'reflection.yaml'))
        self.config = {
            'device': {'id': 'original-device'},
            'policy': {'type': 'ui_venus_2', 'params': {'model_name': 'original-model'}},
            'ep_config': {'step_limit': 30, 'reflection': {'params': {'model_name': 'reviewer'}}},
            'app_mapping': {'相册': 'gallery.package'},
        }

    def write(self, name, value):
        (self.root / name).write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')

    def test_no_overrides_preserve_inline_settings(self):
        self.write('episode.yaml', self.config)
        self.assertEqual(load_config(self.args), self.config)

    def test_external_mapping_and_cli_overrides_apply_to_policy_and_reflection(self):
        self.config['app_mapping'] = {'config_file': 'apps.yaml'}
        self.write('episode.yaml', self.config)
        self.write('apps.yaml', {'app_mapping': {'微信': 'wechat.package'}})
        self.write('reflection.yaml', {'reflection': {'params': {'max_retries': 3}}})
        self.args.device_id, self.args.step_limit = 'new-device', 5
        self.args.model_host = 'legacy-host'
        self.args.model_url, self.args.model_name = 'http://localhost:1234/v1', 'new-model'
        self.args.reflection = True

        result = load_config(self.args)
        self.assertEqual(result['device']['id'], 'new-device')
        self.assertEqual(result['ep_config']['step_limit'], 5)
        self.assertEqual(result['app_mapping'], {'微信': 'wechat.package'})
        self.assertEqual(result['policy']['params'], {
            'model_host': 'legacy-host', 'model_url': self.args.model_url, 'model_name': 'new-model'})
        self.assertEqual(result['ep_config']['reflection']['params'], {
            'max_retries': 3, 'model_url': self.args.model_url, 'model_name': 'new-model'})

    def test_missing_mapping_warns_and_missing_reflection_fails(self):
        self.config['app_mapping'] = {'config_file': 'missing.yaml'}
        self.write('episode.yaml', self.config)
        with self.assertLogs('main', level='WARNING'):
            self.assertEqual(load_config(self.args)['app_mapping'], {})
        self.args.reflection = True
        with self.assertLogs('main', level='WARNING'), self.assertRaises(FileNotFoundError):
            load_config(self.args)
