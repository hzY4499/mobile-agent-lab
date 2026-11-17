"""Hosted API requests preserve native GUI parsing without local vLLM fields."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

FW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FW))
from policy.ui_venus_2_policy import UIVenus2Policy


class DeepSeekProfileTests(unittest.TestCase):
    def test_profile_reaches_sdk_and_native_action_parser(self):
        config = yaml.safe_load((FW / 'config/deepseek_official.yaml').read_text())
        self.assertEqual(config['policy']['type'], 'ui_venus_2')
        params = config['policy']['params']
        self.assertEqual(params['api_key'], '')
        self.assertEqual(params['api_parameters'], {
            'max_tokens': 16384, 'reasoning_effort': 'max',
            'extra_body': {'thinking': {'type': 'enabled'}}})
        with patch('policy.ui_venus_2_policy.OpenAI') as sdk:
            sdk.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content='<action>Click(point=(100, 200))</action>', reasoning_content='check'))])
            policy = UIVenus2Policy(None, **params)
            messages = [{'role': 'user', 'content': 'fixture'}]
            response = policy._call_model(messages)
            self.assertEqual(sdk.return_value.chat.completions.create.call_args.kwargs,
                             dict(model='deepseek-flash', messages=messages, **params['api_parameters']))
            sdk.assert_called_once_with(base_url='https://api.deepseek.com', api_key=policy.api_key,
                                        timeout=120.0, max_retries=0)
            self.assertIn('<think>check</think>', response)
            policy._current_image_size = (1000, 1000)
            self.assertEqual(policy._build_result(response)[0]['action_pos'], [[100, 200]])

    def test_profile_cannot_replace_model_or_messages(self):
        for name, value in [('model', 'wrong'), ('messages', [])]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                UIVenus2Policy(None, api_parameters={name: value})

    def test_default_vllm_parameters_are_preserved(self):
        with patch('policy.ui_venus_2_policy.OpenAI') as sdk:
            sdk.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='<action>PressHome()</action>'))])
            UIVenus2Policy(None, model_url='http://fixture/v1', model_name='local')._call_model([])
            request = sdk.return_value.chat.completions.create.call_args.kwargs
            self.assertEqual(request['temperature'], 0.6)
            self.assertEqual(request['extra_body'], {'repetition_penalty': 1.05, 'frequency_penalty': 0.3})
