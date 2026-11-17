# Recorded native Venus runs

This optional entry point uses `ui_venus_2` and its existing processor, system prompt,
action parser, ADB execution and trajectory. It adds evidence around the run; it
introduces no web tools, orchestration framework or new GUI action protocol.

## Official DeepSeek profile

Use the existing Python environment from the repository setup. Install Android
platform-tools (`adb`), connect a device and ensure `adb devices` reports `device`.
The recorder requires Android `screenrecord`; `ffprobe` is needed to verify videos
and mark the evidence complete. Recording uses POSIX signals and is intended for
a Linux host attached to the phone.

Set `API_KEY` in your local shell or secret manager, without writing the value
into YAML or Git. From the repository root:

```sh
python scripts/run_recorded_venus.py \
  --config Venus_framework/Venus_framework_mobile/config/deepseek_official.yaml \
  --device-id DEVICE_SERIAL --purpose '打开设置并查看当前页面' \
  --step-limit 10 --model-request-limit 20 \
  --expected-model deepseek-flash --expected-base-url https://api.deepseek.com
```

The profile selects `deepseek-flash`, thinking enabled, `reasoning_effort=max`
and `max_tokens=16384`. Model availability depends on the provider/account. It
replaces the local vLLM sampling extras for this profile only. Images still travel
through the native screenshot message path; GUI output still uses `<action>`.
This does not enable native function calling or guarantee benchmark success.

The ordinary `main.py` entry point remains available without recording. Both
entry points share configuration loading and command-line overrides.

## Limits and results

- `--trace-dir` defaults to `runs/`; each invocation creates a unique directory.
  `--evidence-dir` instead selects an exact directory and refuses a prior recorded
  run. Keep output directories local and out of Git.
- `--model-request-limit` counts reservations immediately before SDK dispatch.
  Failed dispatches and policy retries count. SDK automatic retries are disabled
  while the budget is attached and restored afterward. GUI steps are independent.
- `--deadline-ms` is an absolute Unix timestamp in milliseconds. It bounds requests
  and interrupts the native loop on the main thread; cleanup may continue after
  the task deadline to finalize and pull video.
- Expected model and base URL are checked before dispatch; response model identity
  is checked after dispatch. Strict request/deadline limits reject reflection,
  since its separate model client is not covered by this budget.
- Cancellation finalizes evidence in `finally`. Completed trajectory rows are
  preserved as partial evidence when the native loop is interrupted. A forced
  process kill or host crash cannot guarantee video finalization.

Outputs include `api-NNN-request.json`, responses/errors, deduplicated `images/`,
observations, initial/final screenshots, `screen-NN.mp4`, `framework.log`, native
pickle and JSON trajectories, `executions.jsonl`, `final-answer.md`, `summary.json`
and (when limits/identity checks are enabled) `budget-progress.json`.
Actual device dispatch is counted: DoubleCLK weighs two, nested tap implementations
are not counted twice. Screenshot-to-album is a separate media mutation.

`evidence_complete` describes file/video/trajectory completeness, independently of
whether the model claims success. Partial, corrupt or unverifiable evidence stays
explicitly incomplete. Startup failure publishes a terminal budget status without
inventing a recording. Task outcomes still need a business-level check.

Known credential values and credential fields are redacted from exported JSON and
framework logs. Screenshots, task text, model output and native pickle trajectories
can still contain private user data: never commit or publish recording directories.

## Offline checks

```sh
python -m unittest discover -s Venus_framework/Venus_framework_mobile/tests -v
```

Tests mock phones/video and model responses, and exercise the actual SDK with an
in-memory HTTP transport. They make no external API requests or phone actions.
