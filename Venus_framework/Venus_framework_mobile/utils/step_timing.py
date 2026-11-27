"""Per-step segmented timing helpers (measurement only, no behaviour change).

All durations are seconds measured with ``time.perf_counter`` (monotonic).

Log contract (stable, machine-parseable)::

    STEP_TIMING {"step": 3, "step_total_s": 9.1234, ...}
    EPISODE_TIMING_SUMMARY {"steps": 12, "segments": {...}}

Each marker is followed by a single-line JSON object. The same data is stored in
``trajectory.pkl.gz`` as ``episode_data[i]['timing']`` and ``trajectory['timing_summary']``.

Command line (run from ``Venus_framework/Venus_framework_mobile``)::

    python -m utils.step_timing logs/agent-traces/<episode_id>/trajectory.pkl.gz
    python -m utils.step_timing logs/agent-traces/<episode_id>          # directory also works
    python -m utils.step_timing --log run.log                           # parse STEP_TIMING log lines
    python -m utils.step_timing <trajectory> --json                     # machine-readable output
"""
import argparse
import json
import math
import os
import statistics
import struct
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

STEP_TIMING_PREFIX = "STEP_TIMING "
EPISODE_SUMMARY_PREFIX = "EPISODE_TIMING_SUMMARY "

now = time.perf_counter

# Segments that are durations (seconds), in display order. Everything else in a
# timing record (counts, sizes) is reported by ``COUNT_FIELDS``.
SEGMENT_FIELDS = (
    "step_total_s",
    "screenshot_s",
    "screenshot_capture_s",
    "screenshot_encode_s",
    "model_s",
    "model_call_s",
    "reflection_s",
    "reflection_judge_s",
    "reflection_retry_model_s",
    "save_screenshot_s",
    "action_s",
    "wait_s",
    "other_s",
)
COUNT_FIELDS = (
    "screenshot_attempts",
    "screenshot_bytes",
    "model_calls",
    "model_attempts",
    "model_http_requests",
    "reflection_rounds",
    "reflection_judge_calls",
    "reflection_judge_attempts",
    "reflection_judge_http_requests",
    "reflection_retry_calls",
    "reflection_retry_attempts",
    "reflection_retry_http_requests",
)


class _TrackedCall:
    def __init__(self):
        self.attempts = 0
        self.http_requests = 0


class _Track:
    def __init__(self, recorder):
        self._recorder = recorder
        self.call = _TrackedCall()

    def __enter__(self):
        self._started = now()
        self._recorder._active = self.call
        return self.call

    def __exit__(self, exc_type, exc, tb):
        self._recorder._active = None
        self._recorder.calls.append({
            "duration_s": now() - self._started,
            "attempts": self.call.attempts,
            "http_requests": self.call.http_requests,
        })
        return False


class CallRecorder:
    """Accumulates model calls made between two ``reset()`` calls.

    ``attempts`` counts the wrapper's own retry loop; ``http_requests`` counts the
    requests actually sent by the OpenAI SDK (it retries internally, which is not
    visible as an attempt). Both start at 0 and are only informational.
    """

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._active: Optional[_TrackedCall] = None

    def reset(self):
        self.calls = []

    def track(self) -> _Track:
        return _Track(self)

    def note_http_request(self):
        if self._active is not None:
            self._active.http_requests += 1

    def attach_http_counter(self, openai_client) -> None:
        """Count SDK-level HTTP requests; silently skipped if the client has no hooks."""
        try:
            openai_client._client.event_hooks["request"].append(
                lambda request: self.note_http_request())
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        calls = list(self.calls)
        return {
            "calls": len(calls),
            "duration_s": sum(c["duration_s"] for c in calls),
            "attempts": sum(c["attempts"] for c in calls),
            "http_requests": sum(c["http_requests"] for c in calls),
        }


def png_size(data: bytes) -> Optional[tuple]:
    """Return ``(width, height)`` from a PNG header without decoding the image."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _round(value):
    return round(value, 4) if isinstance(value, float) else value


def format_step_log(timing: Dict[str, Any]) -> str:
    return STEP_TIMING_PREFIX + json.dumps(
        {k: _round(v) for k, v in timing.items()}, ensure_ascii=True, separators=(",", ":"))


def format_summary_log(summary: Dict[str, Any]) -> str:
    return EPISODE_SUMMARY_PREFIX + json.dumps(summary, ensure_ascii=True, separators=(",", ":"))


def _stats(values: List[float]) -> Dict[str, float]:
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "max": round(max(values), 4),
        "total": round(math.fsum(values), 4),
    }


def summarize(timings: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Mean/median/max/total per segment.

    A segment is only aggregated over the steps that actually have it (for example
    ``wait_s`` is absent on the terminal step and ``reflection_*`` when reflection is
    off), so ``n`` can be smaller than ``steps``.
    """
    records = [t for t in timings if isinstance(t, dict)]
    segments: Dict[str, Any] = {}
    for name in SEGMENT_FIELDS:
        values = [float(t[name]) for t in records if isinstance(t.get(name), (int, float))]
        if values:
            segments[name] = _stats(values)
    counts: Dict[str, Any] = {}
    for name in COUNT_FIELDS:
        values = [t[name] for t in records if isinstance(t.get(name), (int, float))]
        if values:
            counts[name] = {"n": len(values), "mean": round(statistics.fmean(values), 2),
                            "max": max(values), "total": sum(values)}
    return {"steps": len(records), "segments": segments, "counts": counts}


def timings_from_trajectory(trajectory: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for step in trajectory.get("episode_data", []):
        timing = step.get("timing")
        if isinstance(timing, dict):
            result.append(timing)
        elif isinstance(step.get("step_time"), (int, float)):
            result.append({"step": step.get("step"), "step_total_s": float(step["step_time"])})
    return result


def timings_from_log(lines: Iterable[str]) -> List[Dict[str, Any]]:
    result = []
    for line in lines:
        index = line.find(STEP_TIMING_PREFIX)
        if index >= 0:
            try:
                result.append(json.loads(line[index + len(STEP_TIMING_PREFIX):]))
            except ValueError:
                continue
    return result


def format_table(timings: List[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    columns = [n for n in SEGMENT_FIELDS if any(isinstance(t.get(n), (int, float)) for t in timings)]
    short = {n: n[:-2] if n.endswith("_s") else n for n in columns}
    widths = {n: max(len(short[n]), 7) for n in columns}
    lines = ["step " + " ".join(short[n].rjust(widths[n]) for n in columns)]
    for t in timings:
        cells = []
        for n in columns:
            v = t.get(n)
            cells.append((f"{v:.2f}" if isinstance(v, (int, float)) else "-").rjust(widths[n]))
        lines.append(f"{str(t.get('step', '?')).rjust(4)} " + " ".join(cells))
    lines.append("")
    lines.append(f"steps={summary['steps']}  (seconds)")
    lines.append(f"{'segment':<26}{'n':>4}{'mean':>9}{'median':>9}{'max':>9}{'total':>10}")
    for name, s in summary["segments"].items():
        lines.append(f"{name:<26}{s['n']:>4}{s['mean']:>9.2f}{s['median']:>9.2f}{s['max']:>9.2f}{s['total']:>10.2f}")
    total = summary["segments"].get("step_total_s", {}).get("total")
    if total:
        lines.append("")
        lines.append("share of step_total_s:")
        for name in ("screenshot_s", "model_s", "reflection_s", "save_screenshot_s", "action_s", "wait_s", "other_s"):
            s = summary["segments"].get(name)
            if s:
                lines.append(f"  {name:<24}{100 * s['total'] / total:>6.1f}%")
    if summary["counts"]:
        lines.append("")
        lines.append(f"{'count':<34}{'n':>4}{'mean':>13}{'max':>11}{'total':>11}")
        for name, s in summary["counts"].items():
            lines.append(f"{name:<34}{s['n']:>4}{s['mean']:>13.2f}{s['max']:>11}{s['total']:>11}")
    return "\n".join(lines)


def _load_trajectory(path: str) -> Dict[str, Any]:
    if os.path.isdir(path):
        path = os.path.join(path, "trajectory.pkl.gz")
    from utils.pickle_utils import load_gzip_pickle
    return load_gzip_pickle(path)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print per-step timing from a trajectory.pkl.gz (or STEP_TIMING log lines).")
    parser.add_argument("trajectory", nargs="?", help="trajectory.pkl.gz file or its episode directory")
    parser.add_argument("--log", help="read STEP_TIMING lines from a log file instead")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if bool(args.trajectory) == bool(args.log):
        parser.error("give exactly one of TRAJECTORY or --log")
    if args.log:
        with open(args.log, encoding="utf-8", errors="replace") as f:
            timings = timings_from_log(f)
    else:
        # Trajectories are pickles: only open files written by your own runs.
        timings = timings_from_trajectory(_load_trajectory(args.trajectory))
    if not timings:
        print("no timing data found", file=sys.stderr)
        return 1
    summary = summarize(timings)
    if args.json:
        print(json.dumps({"steps": timings, "summary": summary}, ensure_ascii=False, indent=2))
    else:
        print(format_table(timings, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
