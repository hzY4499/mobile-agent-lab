"""Per-episode evidence, adapted from benchmarks/gui_v1/worker.py.

Keep the Huawei benchmark's screenrecord -> SIGINT -> pull workflow and file
names. This recorder is independent of benchmark cases, oracles and sync SDKs.
"""
import base64
import hashlib
import json
import logging
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time
import uuid


class EvidenceRecorder:
    def __init__(self, directory, serial, metadata, secrets=(), request_budget=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.serial = serial
        self.secrets = tuple(s for s in secrets if isinstance(s, str) and len(s) >= 8)
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.video_error = None
        self.segments = []
        self.calls = []
        self.executions = []
        self._dispatch_depth = 0
        self.observations = []
        self.handler = None
        self.started = time.time()
        self.metadata = metadata
        self.restores = []
        self.closed = False
        self.current_step = None
        self.trajectory_summary = {}
        self.request_budget = request_budget
        self.save_summary("RUNNING")

    def sanitize(self, value):
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if str(k).lower() in {
                "api_key", "authorization", "password", "access_token", "secret"
            } else self.sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(v) for v in value]
        if isinstance(value, str):
            if value.startswith("data:image/"):
                try:
                    raw = base64.b64decode(value.split(",", 1)[1], validate=True)
                    digest = hashlib.sha256(raw).hexdigest()
                    folder = self.directory / "images"
                    folder.mkdir(exist_ok=True)
                    name = digest + ".png"
                    (folder / name).write_bytes(raw)
                    return {"image_sha256": digest, "file": "images/" + name}
                except (ValueError, IndexError):
                    return "[Invalid image omitted]"
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return "[" + type(value).__name__ + "]"

    def write(self, name, value):
        path = self.directory / name
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(self.sanitize(value), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def adb(self, *args, timeout=10):
        return subprocess.run(["adb", "-s", self.serial, *args], capture_output=True,
                              check=True, timeout=timeout).stdout

    def screenshot(self, name):
        try:
            data = self.adb("exec-out", "screencap", "-p")
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Invalid screenshot")
            (self.directory / name).write_bytes(data)
        except Exception as error:
            self.write(name + ".error.json", {"error_type": type(error).__name__})

    def observe(self, image, step):
        self.current_step = step
        n = len(self.observations) + 1
        name = f"observation-{n:03}.png"
        (self.directory / name).write_bytes(base64.b64decode(image, validate=True))
        self.observations.append({"file": name, "step": step, "at": time.time()})
        self.save_summary("RUNNING")

    def model_request(self, kwargs):
        n = len(self.calls) + 1
        self.calls.append({"index": n, "step": self.current_step, "started": time.time()})
        self.write(f"api-{n:03}-request.json", kwargs)
        self.save_summary("RUNNING")
        return n

    def model_response(self, n, response=None, error=None):
        record = self.calls[n - 1]
        record["elapsed_s"] = time.time() - record["started"]
        if error is not None:
            record["error_type"] = type(error).__name__
            self.write(f"api-{n:03}-error.json", record)
        else:
            self.write(f"api-{n:03}-response.json", response)
            record["finish_reason"] = (response.get("choices") or [{}])[0].get("finish_reason")
        self.save_summary("RUNNING")

    def attach_client(self, policy):
        client = getattr(policy, "client", None)
        if client is None:
            return
        if hasattr(client, "chat"):
            completions = client.chat.completions
            create = completions.create
            retries = client.max_retries
            if self.request_budget:
                client.max_retries = 0
                self.restores.append(lambda: setattr(client, "max_retries", retries))
            def recorded_create(**kwargs):
                if self.request_budget:
                    kwargs = self.request_budget.admit(kwargs, client)
                n = self.model_request(kwargs)
                try:
                    response = create(**kwargs)
                    self.model_response(n, response.model_dump(mode="json"))
                    if self.request_budget:
                        self.request_budget.response(response)
                    return response
                except BaseException as error:
                    self.model_response(n, error=error)
                    raise
            completions.create = recorded_create
            self.restores.append(lambda: setattr(completions, "create", create))

    def attach_device(self, handler):
        """Observe dispatch at Device methods, after action parsing/validation.

        Nested implementations (double_tap calling tap) count only the outer
        dispatch. A call is counted even if the device returns failure, matching
        the benchmark's attempted GUI action definition.
        """
        self._save_executions()
        device = handler.device_manager.get_device(handler.device_id)
        if device is None:
            return
        names = {'tap': ('CLK', 1), 'swipe': ('SWIPE', 1), 'input_text': ('INPUT', 1),
                 'pressback': ('BACK', 1), 'presshome': ('PressHome', 1),
                 'pressmenu': ('PressMenu', 1), 'pressenter': ('PressEnter', 1),
                 'longpress': ('LongPress', 1), 'launch_app': ('REOPEN', 1),
                 'double_tap': ('DoubleCLK', 2), 'get_screenshot_to_album': ('get_screenshot', 0)}
        for name, (kind, weight) in names.items():
            original = getattr(device, name, None)
            if not callable(original):
                continue
            def observed(*args, _original=original, _kind=kind, _weight=weight, **kwargs):
                if self._dispatch_depth:
                    return _original(*args, **kwargs)
                if self.request_budget:
                    self.request_budget.check_deadline()
                record = {'step': handler.runtime_context.step,
                          'action': {'action_type': _kind, 'arguments': list(args), 'keywordArguments': kwargs},
                          'startedAtMs': int(time.time() * 1000), 'endedAtMs': None,
                          'success': None, 'guiActionCount': _weight}
                if _kind == 'get_screenshot':
                    record['mutatesMedia'] = True
                self.executions.append(record)
                self._save_executions()
                self._dispatch_depth += 1
                try:
                    result = _original(*args, **kwargs)
                    record['success'] = bool(result)
                    return result
                except BaseException as error:
                    record['error_type'] = type(error).__name__
                    raise
                finally:
                    self._dispatch_depth -= 1
                    record['endedAtMs'] = int(time.time() * 1000)
                    self._save_executions()
            setattr(device, name, observed)
            self.restores.append(lambda name=name, original=original: setattr(device, name, original))

    def _save_executions(self):
        path = self.directory / 'executions.jsonl'
        temp = path.with_suffix('.jsonl.tmp')
        temp.write_text(''.join(json.dumps(self.sanitize(item), ensure_ascii=False) + '\n'
                                for item in self.executions), encoding='utf-8')
        temp.replace(path)

    def start(self, policy):
        recorder = self
        class RedactedFormatter(logging.Formatter):
            def format(self, record):
                return recorder.sanitize(super().format(record))
        self.handler = logging.FileHandler(self.directory / "framework.log", encoding="utf-8")
        self.handler.setFormatter(RedactedFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logging.getLogger().addHandler(self.handler)
        self.write("run-config.json", self.metadata)
        self.screenshot("initial.png")
        self.attach_client(policy)
        self.thread = threading.Thread(target=self._video, name="venus-screenrecord", daemon=True)
        self.thread.start()
        if not self.ready.wait(10) or self.video_error:
            raise RuntimeError("Screen recording failed to start; inspect summary.json")
        logging.getLogger(__name__).info("本轮完整记录目录: %s", self.directory)

    def _video(self):
        # Same 180-second segments and remote SIGINT finalization as the Huawei
        # worker. UUID scopes remote files to this recorder; never pkill others.
        token = uuid.uuid4().hex
        number = 0
        try:
            while not self.stop_event.is_set():
                number += 1
                remote = f"/data/local/tmp/venus-evidence-{token}-{number}.mp4"
                local = self.directory / f"screen-{number:02}.mp4"
                started = time.time()
                proc = subprocess.Popen([
                    "adb", "-s", self.serial, "shell",
                    f"echo $$; exec screenrecord --size 720x1600 --bit-rate 2000000 --time-limit 180 {remote}",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                pid = ""
                try:
                    if select.select([proc.stdout], [], [], 5)[0]:
                        pid = proc.stdout.readline().decode().strip()
                    if not pid.isdigit():
                        raise RuntimeError("No screenrecord PID")
                    time.sleep(0.6)
                    if proc.poll() is not None:
                        raise RuntimeError("screenrecord exited at startup")
                    self.ready.set()
                    while proc.poll() is None:
                        if self.stop_event.wait(.25):
                            self.adb("shell", "kill", "-2", pid)
                            break
                    try:
                        _, stderr = proc.communicate(timeout=12)
                    except subprocess.TimeoutExpired:
                        self.adb("shell", "kill", "-2", pid)
                        proc.terminate()
                        _, stderr = proc.communicate(timeout=5)
                    stopped = time.time()
                    (self.directory / f"screen-{number:02}.stderr").write_text(
                        self.sanitize(stderr.decode(errors="replace")), encoding="utf-8")
                    self.adb("pull", remote, str(local), timeout=30)
                    segment = {"file": local.name, "started": started, "stopped": stopped,
                               "bytes": local.stat().st_size, "resolution": "720x1600"}
                    if shutil.which("ffprobe"):
                        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                                "-show_entries", "stream=codec_name,width,height:format=duration",
                                                "-of", "json", str(local)], capture_output=True, timeout=10)
                        segment["valid"] = probe.returncode == 0 and bool(json.loads(probe.stdout).get("streams"))
                        if not segment["valid"]:
                            raise RuntimeError("Invalid video")
                    else:
                        segment["valid"] = None
                    self.segments.append(segment)
                    self.adb("shell", "rm", "-f", remote)
                    if not self.stop_event.is_set() and stopped - started < 170:
                        raise RuntimeError("screenrecord ended before its segment limit")
                finally:
                    if proc.poll() is None:
                        if pid.isdigit():
                            try:
                                self.adb("shell", "kill", "-2", pid)
                            except Exception:
                                pass
                        proc.terminate()
                        try:
                            proc.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.communicate()
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe:
                            pipe.close()
        except Exception as error:
            self.video_error = type(error).__name__
            logging.getLogger(__name__).error("录屏失败 (%s)", self.video_error)
        finally:
            self.ready.set()

    def save_summary(self, status, outcome=None):
        required = ['framework.log', 'initial.png', 'final.png', 'run-config.json',
                    'trajectory.json', 'final-answer.md']
        required += [o['file'] for o in self.observations]
        for call in self.calls:
            n = call['index']
            required += [f'api-{n:03}-request.json',
                         f"api-{n:03}-{'error' if call.get('error_type') else 'response'}.json"]
        missing = [name for name in required if not (self.directory / name).is_file()]
        self.write("summary.json", {"status": status, "started": self.started,
                   "updated": time.time(), "metadata": self.metadata,
                   "native_result": outcome, "calls": self.calls,
                   "executions": self.executions,
                   "guiActionCount": sum(x['guiActionCount'] for x in self.executions),
                   "trajectory": self.trajectory_summary,
                   "observations": self.observations, "video_segments": self.segments,
                   "recording_error": self.video_error,
                   "missing_files": missing,
                   "evidence_complete": status != "RUNNING" and not missing and
                       not self.trajectory_summary.get('partial', True) and
                       not self.trajectory_summary.get('export_error') and not self.video_error and
                       bool(self.segments) and all(s.get("valid") is True for s in self.segments)})

    def close(self, outcome=None, trajectory=None):
        if self.closed:
            return
        self.closed = True
        try:
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=55)
                if self.thread.is_alive():
                    self.video_error = "RecorderFinalizationTimeout"
            self.screenshot("final.png")
            if trajectory is not None:
                self.trajectory_summary = {
                    "episode_length": trajectory.get("episode_length"),
                    "run_time": trajectory.get("run_time"),
                    "is_successful": trajectory.get("is_successful"),
                    "termination_reason": trajectory.get("termination_reason"),
                    "export_error": trajectory.get("export_error"),
                    "partial": trajectory.get("partial", False),
                    "trajectory_complete": not trajectory.get("partial", False),
                    "decisions": [{"step": s.get("step"), "action": s.get("action"),
                                   "success": s.get("success")} for s in trajectory.get("episode_data", [])],
                }
                self.write("trajectory.json", trajectory)
                answer = trajectory.get("final_answer", "")
                (self.directory / "final-answer.md").write_text(self.sanitize(answer), encoding="utf-8")
            status = "COMPLETED" if outcome else "INTERRUPTED"
            self.save_summary(status, outcome)
            text = (f"# 本轮运行记录\n\n目录：{self.directory}\n\n"
                    f"运行返回值：{outcome}\n\n录屏错误：{self.video_error or '无'}\n\n"
                    "- 视频：screen-01.mp4（长任务每180秒续录 screen-02.mp4 等）\n"
                    "- 日志：framework.log\n- 初始/结束画面：initial.png、final.png\n"
                    "- 每次模型观测：observation-NNN.png；执行步截图：traces/<episode>/screenshots/\n"
                    "- 模型请求/响应：api-NNN-request.json、api-NNN-response.json\n"
                    "- 可读轨迹：trajectory.json；原始轨迹：traces/<episode>/trajectory.pkl.gz\n"
                    "- 最终回答：final-answer.md；运行汇总：summary.json\n\n"
                    "请求中的图片改为 images/ 文件引用；不保存鉴权请求头。\n"
                    "模型声明完成与证据是否完整分别记录；业务结果仍需复核。\n")
            (self.directory / "README.md").write_text(self.sanitize(text), encoding="utf-8")
        finally:
            for restore in reversed(self.restores):
                restore()
            if self.handler:
                logging.getLogger().removeHandler(self.handler)
                self.handler.close()
