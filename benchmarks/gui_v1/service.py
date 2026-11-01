import copy
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import requests
import yaml

from .common import EnvironmentIssue, Interrupted, metric_sum, now, parse_metrics, write

CONFIG = Path(__file__).with_name('config.yaml')

def load_config(path=CONFIG, temperature=None):
    config = yaml.safe_load(Path(path).read_text())
    if temperature is not None:
        config['sampling']['temperature'] = temperature
    if config['recipient'] != 'Y_Yao' or config['search'] != '天竺葵':
        raise ValueError('当前自动准备模板仅适配Y_Yao和天竺葵；修改对象需先校准模板。')
    if config['chat_history'] != 'preserve':
        raise ValueError('当前支持preserve；自动删除聊天消息尚未实现。')
    if config['resolution'] != [1080, 2400]:
        raise ValueError('当前页面模板仅适配1080×2400。')
    s = config['sampling']
    if not 0 <= s['temperature'] <= 2 or not 0 < s['top_p'] <= 1 or s['max_tokens'] < 1:
        raise ValueError('temperature应为0–2、top_p为(0,1]、max_tokens为正数。')
    for key in ['atomic', 'end_to_end']:
        if config[key]['steps'] < 1 or config[key]['seconds'] < 1:
            raise ValueError('步数和时间预算必须为正数。')
    return config

def request_parameters(config, model):
    s = config['sampling']
    return {'model': config['models'][model]['name'], 'temperature': s['temperature'],
            'max_tokens': s['max_tokens'], 'top_p': s['top_p'], 'seed': s['seed'],
            'presence_penalty': config['models'][model]['presence_penalty'],
            'extra_body': {k: s[k] for k in ['top_k', 'min_p', 'repetition_penalty', 'frequency_penalty']}}

class Service:
    def __init__(self, config):
        self.config = config
        self.settings = config['service']
        self.base = self.settings['url'].removesuffix('/v1')
        self.key = os.environ.get('API_KEY') or os.environ.get('MODEL_API_KEY')
        if not self.key:
            p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                self.settings['ssh_host'], 'cat ' + shlex.quote(self.settings['key_file'])],
                               capture_output=True, text=True, timeout=25)
            if p.returncode or not p.stdout.strip():
                raise EnvironmentIssue('无法通过SSH读取服务密钥；请检查h20连接，或设置API_KEY。')
            self.key = p.stdout.strip()
        self.http = requests.Session()
        self.http.headers['Authorization'] = 'Bearer ' + self.key

    def clean(self, value):
        return str(value).replace(self.key, '[REDACTED]')

    def get(self, route):
        r = self.http.get(self.base + route, timeout=10)
        r.raise_for_status()
        return r

    def metrics(self):
        return parse_metrics(self.get('/metrics').text)

    def ensure(self, model, out, switch=True, stopped=lambda: False):
        if switch:
            command = shlex.join(['python3', self.settings['manager'], model])
            p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                                self.settings['ssh_host'], command], capture_output=True, text=True, timeout=120)
            write(out / 'service-switch.json', {'at': now(), 'code': p.returncode,
                  'stdout': self.clean(p.stdout), 'stderr': self.clean(p.stderr)})
            if p.returncode:
                raise EnvironmentIssue('模型切换失败；详情见service-switch.json。不会停止其他容器。')
        deadline = time.monotonic() + (self.settings['ready_timeout'] if switch else 15)
        expected = self.config['models'][model]['name']
        while True:
            if stopped():
                raise Interrupted('已请求停止')
            try:
                self.get('/health')
                models = self.get('/v1/models').json()
                if expected not in [d['id'] for d in models['data']]:
                    raise EnvironmentIssue('实际服务模型与所选模型不同。')
                info = self.get('/server_info?config_format=json').json()
                config = info.get('vllm_config', {})
                if not isinstance(config, dict):
                    raise EnvironmentIssue('服务未提供可核验的vLLM JSON配置。')
                cache = config.get('cache_config', {})
                if not cache.get('num_gpu_blocks', 0) > 0:
                    raise EnvironmentIssue('未发现KV Cache GPU块分配，暂停测试。')
                if cache.get('enable_prefix_caching') is not True:
                    raise EnvironmentIssue('Prefix Caching与预期true不同，暂停测试。')
                metrics = self.metrics()
                if metric_sum(metrics, 'vllm:num_requests_running') or metric_sum(metrics, 'vllm:num_requests_waiting'):
                    raise EnvironmentIssue('服务仍有运行/等待请求。')
                write(out / 'server-models.json', models)
                write(out / 'server-info.json', json.loads(self.clean(json.dumps(info))))
                write(out / 'server-version.json', self.get('/version').json())
                write(out / 'metrics-before.json', metrics)
                return
            except (requests.RequestException, EnvironmentIssue) as e:
                if time.monotonic() >= deadline:
                    raise EnvironmentIssue('服务未就绪：' + self.clean(e))
                print('等待模型服务就绪…', flush=True)
                time.sleep(5)

    def warmup(self, model, out):
        params = request_parameters(self.config, model)
        params['max_tokens'] = self.config['warmup_tokens']
        params['messages'] = [{'role': 'user', 'content': '请简短回答：1加1等于多少？'}]
        payload = copy.deepcopy(params)
        payload.update(payload.pop('extra_body'))
        write(out / 'warmup-request.json', params)
        write(out / 'warmup-http-request.json', payload)
        r = self.http.post(self.base + '/v1/chat/completions', json=payload, timeout=120)
        r.raise_for_status()
        response = r.json()
        if response.get('model') != params['model']:
            raise EnvironmentIssue('预热返回模型不匹配。')
        write(out / 'warmup-response.json', response)
