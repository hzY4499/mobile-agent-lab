"""Per-job model identity, request budget and wall deadline for recorded runs.

Reservations happen immediately before SDK dispatch, with SDK retries disabled.
Failed requests and policy format retries consume reservations. This deliberately
counts a dispatch attempt even if the SDK rejects it before reaching the network.
"""
import json
import math
from pathlib import Path
import threading
import time


class RequestBudgetStop(KeyboardInterrupt):
    """Bypass policy retry handlers without losing the recorder's finally block."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class RequestBudget:
    def __init__(self, directory, *, model_request_limit=None, deadline_ms=None,
                 expected_model=None, expected_base_url=None):
        if model_request_limit is not None and (type(model_request_limit) is not int or model_request_limit < 1):
            raise ValueError('model_request_limit must be a positive integer')
        if deadline_ms is not None and (not math.isfinite(deadline_ms) or deadline_ms <= 0):
            raise ValueError('deadline_ms must be a positive absolute timestamp')
        self.path = Path(directory) / 'budget-progress.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit, self.deadline_ms = model_request_limit, deadline_ms
        self.expected_model, self.expected_base_url = expected_model, expected_base_url
        self.requests, self.reason, self.terminal = 0, None, False
        self.model = {'name': expected_model, 'url': expected_base_url}
        self.lock = threading.RLock()
        self.persist()

    def persist(self):
        with self.lock:
            data = {'schemaVersion': 1, 'modelRequests': self.requests,
                    'modelRequestLimit': self.limit, 'deadlineMs': self.deadline_ms,
                    'terminal': self.terminal,
                    'status': 'STOPPED' if self.reason else ('COMPLETED' if self.terminal else 'RUNNING'),
                    'reason': self.reason, 'model': self.model, 'updatedAtMs': int(time.time() * 1000),
                    'countDefinition': 'SDK dispatch reservations; max_retries=0; failures and format retries count'}
            temp = self.path.with_suffix('.json.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.path)

    def stop(self, reason):
        self.reason = reason
        self.persist()
        raise RequestBudgetStop(reason)

    def check_deadline(self):
        if self.deadline_ms is not None and time.time() * 1000 >= self.deadline_ms:
            self.stop('deadline_exceeded')

    def admit(self, request, sdk_client):
        with self.lock:
            self.check_deadline()
            model, url = request.get('model'), str(sdk_client.base_url).rstrip('/')
            if self.expected_model and model != self.expected_model:
                self.stop('model_identity_mismatch')
            if self.expected_base_url and url != self.expected_base_url.rstrip('/'):
                self.stop('endpoint_mismatch')
            if self.limit is not None and self.requests >= self.limit:
                self.stop('model_request_limit')
            self.model = {'name': model, 'url': url}
            self.requests += 1
            self.persist()
            kwargs = dict(request)
            if self.deadline_ms is not None:
                remaining = max(.001, (self.deadline_ms - time.time() * 1000) / 1000)
                timeout = kwargs.get('timeout', getattr(sdk_client, 'timeout', 120.0))
                # httpx.Timeout is accepted by OpenAI too. An absolute run alarm
                # provides a second bound beyond the individual transport timeout.
                current = timeout if isinstance(timeout, (int, float)) else getattr(timeout, 'read', None)
                kwargs['timeout'] = min(remaining, current) if current is not None else remaining
            return kwargs

    def response(self, response):
        if self.expected_model and getattr(response, 'model', None) != self.expected_model:
            self.stop('response_model_identity_mismatch')

    def finish(self, reason=None):
        if reason and reason not in {'success', 'call_user'} and not self.reason:
            self.reason = reason
        self.terminal = True
        self.persist()
