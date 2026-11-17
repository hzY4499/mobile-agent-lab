"""External Huawei-style recording wrapper. No changes to the agent loop."""
import gzip
import logging
import os
from pathlib import Path
import pickle
import signal
import threading
import time

from .episode_evidence import EvidenceRecorder
from .request_budget import RequestBudgetStop


def run_recorded_episode(handler, purpose, directory, metadata=None, request_budget=None):
    """Wrap an existing handler instance, restoring all observers on exit."""
    secrets = [os.environ.get(k, '') for k in ('API_KEY', 'MODEL_API_KEY', 'OPENAI_API_KEY')]
    secrets += [getattr(handler.policy, 'api_key', ''),
                getattr(getattr(handler.policy, 'client', None), 'api_key', '')]
    recorder = EvidenceRecorder(directory, handler.device_id, dict(metadata or {}, purpose=purpose), secrets,
                                request_budget=request_budget)
    request_screen = handler.req_from_client
    before = set(Path(handler.trace_dir).glob('*/trajectory.pkl.gz'))
    outcome = None
    previous_term = None
    previous_alarm = None
    trajectory = None

    def observed_screen():
        if request_budget:
            request_budget.check_deadline()
        if recorder.video_error:
            raise RuntimeError('Screen recording failed; inspect summary.json')
        data = request_screen()
        if request_budget:
            request_budget.check_deadline()
        if data.get('screenshot_str'):
            recorder.observe(data['screenshot_str'], handler.runtime_context.step)
        return data

    if threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)
        def interrupt(signum, frame):
            if request_budget:
                request_budget.check_deadline()
            raise KeyboardInterrupt('Run terminated; saving recording')
        signal.signal(signal.SIGTERM, interrupt)
        if request_budget and request_budget.deadline_ms is not None:
            previous_alarm = (signal.getsignal(signal.SIGALRM), signal.getitimer(signal.ITIMER_REAL))
            def deadline(signum, frame):
                request_budget.stop('deadline_exceeded')
            signal.signal(signal.SIGALRM, deadline)
            signal.setitimer(signal.ITIMER_REAL, max(.001, request_budget.deadline_ms / 1000 - time.time()))
    try:
        if request_budget:
            request_budget.check_deadline()
        recorder.start(handler.policy)
        recorder.attach_device(handler)
        handler.req_from_client = observed_screen
        outcome = handler.run(purpose)
        if request_budget and request_budget.reason:
            outcome = (False, request_budget.reason, None)
        return outcome
    except RequestBudgetStop as error:
        outcome = (False, error.reason, None)
        return outcome
    finally:
        # Node's deadline fallback can send SIGTERM at the same instant as our
        # alarm. Finalization must survive the second signal to close the MP4.
        if previous_term is not None:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if previous_alarm is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
        handler.req_from_client = request_screen
        try:
            trajectory = dict(goal=purpose, episode_data=[], is_successful=False,
                              termination_reason='interrupted_before_trajectory',
                              partial=True,
                              final_answer=getattr(handler, 'final_answer', ''))
            try:
                paths = set(Path(handler.trace_dir).glob('*/trajectory.pkl.gz')) - before
                if paths:
                    path = max(paths, key=lambda p: p.stat().st_mtime_ns)
                    # Only files created by this local run, never untrusted downloads.
                    with gzip.open(path, 'rb') as file:
                        trajectory = dict(pickle.load(file))
                    trajectory.setdefault('partial', False)
                    trajectory['original_trajectory_file'] = str(path)
                    if 'final_answer' not in trajectory:
                        steps = trajectory.get('episode_data', [])
                        action = steps[-1].get('action') if steps else None
                        trajectory['final_answer'] = (action or {}).get('input', '')
                else:
                    # Native Venus used to lose all completed trajectory rows on
                    # cancellation. Preserve its telemetry without inventing the
                    # unfinished row; executions.jsonl records in-flight actions.
                    partial = getattr(handler, '_partial_episode_data', None)
                    active = getattr(handler, '_active_episode_dir', None)
                    if isinstance(partial, list) and isinstance(active, str):
                        trajectory.update(episode_data=list(partial),
                                          episode_length=getattr(handler.runtime_context, 'step', None),
                                          run_time=time.time()-getattr(handler, '_episode_started_at', time.time()),
                                          termination_reason=request_budget.reason if request_budget and request_budget.reason else 'interrupted',
                                          partial=True)
                        path = Path(active) / 'trajectory.pkl.gz'
                        with gzip.open(path, 'wb') as file:
                            pickle.dump(trajectory, file)
                        trajectory['original_trajectory_file'] = str(path)
            except Exception as error:
                trajectory['export_error'] = type(error).__name__
                logging.getLogger(__name__).error('轨迹读取失败 (%s)', type(error).__name__)
            if request_budget:
                trajectory['model_request_budget'] = {
                    'modelRequests': request_budget.requests, 'limit': request_budget.limit,
                    'deadlineMs': request_budget.deadline_ms, 'reason': request_budget.reason}
                if request_budget.reason:
                    trajectory.update(is_successful=False, termination_reason=request_budget.reason)
            recorder.close(outcome, trajectory)
        finally:
            if request_budget:
                request_budget.finish(outcome[1] if outcome else 'interrupted')
            if previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)
            if previous_alarm is not None:
                signal.signal(signal.SIGALRM, previous_alarm[0])
                signal.setitimer(signal.ITIMER_REAL, *previous_alarm[1])
            logging.getLogger(__name__).info('记录已保存: %s', directory)
