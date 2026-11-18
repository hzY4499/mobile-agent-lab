"""Run one native GUI task with evidence and optional request/deadline limits."""
import argparse
from datetime import datetime
import logging
from pathlib import Path
import re
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
FW = REPO / 'Venus_framework/Venus_framework_mobile'
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(FW))

from app.run_handler import RunHandler
from benchmarks.recorded_episode import run_recorded_episode
from benchmarks.request_budget import RequestBudget
from main import load_config, setup_logging


def main():
    parser = argparse.ArgumentParser(description='Run a recorded native Venus GUI task')
    parser.add_argument('--config', default=str(FW / 'config/deepseek_official.yaml'))
    parser.add_argument('--purpose', required=True)
    parser.add_argument('--trace-dir', default=str(REPO / 'runs'))
    parser.add_argument('--device-id')
    parser.add_argument('--step-limit', type=int)
    parser.add_argument('--model-url')
    parser.add_argument('--model-name')
    parser.add_argument('--log-file')
    parser.add_argument('--evidence-dir', help='Exact job evidence directory, without a generated subdirectory')
    parser.add_argument('--model-request-limit', type=int)
    parser.add_argument('--deadline-ms', type=int, help='Absolute Unix time in milliseconds')
    parser.add_argument('--expected-model')
    parser.add_argument('--expected-base-url')
    parser.add_argument('--reflection', action='store_true')
    parser.add_argument('--reflection-config', default=str(FW / 'config/reflection.yaml'))
    args = parser.parse_args()
    setup_logging(args.log_file)
    config = load_config(args)
    task = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', args.purpose)[:10]
    name = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + task + '_' + uuid.uuid4().hex[:8]
    directory = Path(args.evidence_dir).resolve() if args.evidence_dir else Path(args.trace_dir).resolve() / name
    if (directory / 'summary.json').exists() or (directory / 'run-config.json').exists():
        raise FileExistsError('Refusing to overwrite a previous recorded episode')
    budget = None
    if any(value is not None for value in (args.model_request_limit, args.deadline_ms,
                                           args.expected_model, args.expected_base_url)):
        budget = RequestBudget(directory, model_request_limit=args.model_request_limit,
                               deadline_ms=args.deadline_ms, expected_model=args.expected_model,
                               expected_base_url=args.expected_base_url)
    logging.info('本轮完整记录目录: %s', directory)
    try:
        if config['policy']['type'] != 'ui_venus_2':
            raise ValueError('Recorded runs require policy type ui_venus_2')
        if budget and config['ep_config'].get('reflection'):
            raise ValueError('Recorded limits and identity checks do not cover the separate reflection model')
        handler = RunHandler(
            device_id=config['device']['id'], trace_dir=str(directory / 'traces'),
            policy_type=config['policy']['type'], ep_config=config['ep_config'],
            app_mapping=config.get('app_mapping', {}),
            **config['policy'].get('params', {}))
    except BaseException:
        # A constructor failure happens before the evidence wrapper can finalize.
        # Publish the zero/request reservation count, without inventing a video
        # or a complete trajectory for a run that never entered the agent loop.
        if budget:
            budget.finish('startup_failed')
        logging.exception('Venus startup failed before the recorded episode')
        return 1
    outcome = run_recorded_episode(handler, args.purpose, directory, metadata={'config': config},
                                   request_budget=budget)
    logging.info('运行返回值: %s', outcome)
    if handler.final_answer:
        logging.info('>> %s', handler.final_answer)
    return 0 if outcome[1] in {'success', 'call_user'} else 1


if __name__ == '__main__':
    sys.exit(main())
