import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import requests

from benchmarks.gui_v1.common import REPO, read, write
from benchmarks.gui_v1.oracle import assess
from benchmarks.gui_v1.reporting import build
from benchmarks.gui_v1.service import load_config


class ReviewHTTPTests(unittest.TestCase):
    def test_review_page_and_receipt_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / 'manifest.json', {
                'models': ['qwen38'], 'cases': ['E01'], 'repetitions': 1,
                'config': load_config(),
            })
            folder = root / 'qwen38/runs/E01-r1'
            write(folder / 'summary.json', {
                'case': 'E01', 'rep': 1, 'status': 'COMPLETED',
                'native_result': [True, 'success', None],
                'gui_action_count': 5, 'wall_s': 20, 'decisions': [],
            })
            write(folder / 'assessment.json', assess(folder))
            build(root)
            process = subprocess.Popen(
                [sys.executable, '-u', '-m', 'benchmarks.gui_v1.cli',
                 'review', str(root), '--port', '0'],
                cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            http = requests.Session()
            http.trust_env = False
            try:
                url = process.stdout.readline().strip().split('：', 1)[1]
                base, query = url.split('?', 1)
                token = query.split('=', 1)[1]
                self.assertEqual(http.get(base, timeout=5).status_code, 403)
                response = http.get(base, params={
                    'token': token, 'run': 'qwen38/runs/E01-r1'}, timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertIn('<form', response.text)
                self.assertIn('E01-r1', response.text)
                self.assertEqual(http.get(base + 'file', params={
                    'token': token, 'path': '../outside.txt'}, timeout=5).status_code, 404)
                payload = {'run': 'qwen38/runs/E01-r1', 'verdict': 'PASS',
                           'note': '测试夹具：本轮新增准确消息'}
                save_url = base + 'save?token=' + token
                self.assertEqual(http.post(save_url, data=payload, timeout=5,
                                           allow_redirects=False).status_code, 303)
                self.assertEqual(read(folder / 'assessment.json')['result'], 'PENDING_RECEIVER')
                payload['receiver'] = 'yes'
                self.assertEqual(http.post(save_url, data=payload, timeout=5,
                                           allow_redirects=False).status_code, 303)
                self.assertEqual(read(folder / 'assessment.json')['result'], 'PASS')
                self.assertEqual(len(read(folder / 'review-history.json')), 2)
                self.assertIn('PASS', (root / 'REPORT.md').read_text())
            finally:
                http.close()
                process.send_signal(signal.SIGINT)
                _, errors = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(errors, '')


if __name__ == '__main__':
    unittest.main()
