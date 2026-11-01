# Mobile Agent Lab

本项目在 UI-Venus 移动端框架上开发 Qwen 手机 GUI agent，现有基线包含 Qwen3.5-4B / Qwen3.8-27B 的 GUI benchmark、请求记录、结果复核和续跑逻辑。

目前已整理进仓库的是现有 GUI 代码和联网能力设计。`web_search`、`web_fetch`、直接媒体读取和投机解码尚未在本项目实现或完成真机验收。

## 仓库内容

- `Venus_framework/Venus_framework_mobile/`：官方移动端框架及本地采样参数支持。
- `benchmarks/gui_v1/`：现有 60 次测试运行、准备、记录和复核程序。
- `run_gui_benchmark.sh`：GUI benchmark 启动脚本。
- `run_android_agent.sh`：单任务启动脚本。
- `LOCAL_SETUP.md`：已有本机部署记录，包含依赖本机设备和 H20 的配置。
- `docs/hybrid-ui-web-plan-20261101.md`：联网搜索、手机 API 和加速的研究与实施方案。

## 本机运行与验证

运行依赖原部署环境的 Python 虚拟环境、ADB、测试手机和已部署的模型服务。虚拟环境不进入版本库。请先参照 `LOCAL_SETUP.md` 和 `benchmarks/gui_v1/README.md` 核对本机路径；克隆仓库本身不会配置模型、手机或凭据。

```bash
python -m unittest discover -s benchmarks/gui_v1/tests -v
```

`benchmarks/gui_v1/assets/` 包含本机设备页面的截图校准素材，已从 Git 排除。当前电脑的新仓库中保留了本地副本；在另一台电脑执行真机准备流程前，需要自行校准并提供这些素材。截图、录屏、API 密钥、虚拟环境、实验大文件均不提交。

## 上游与本地开发

上游来自 [inclusionAI/UI-Venus](https://github.com/inclusionAI/UI-Venus)，基线提交为 `1ffbaf6`，原始 README 保存在 `UPSTREAM_README.md`。原有许可证和归属信息保留在 `LICENSE` 等上游文件中。

官方仓库配置为 `upstream`，本项目自己的仓库配置为 `origin`。开发分支采用 `codex/` 前缀。

当前仓库覆盖实际运行的 UI-Venus 工程和本次设计报告。`/home/kojoker/Documents/ChatGPT/h20` 中其他研究材料、桌面历史测试结果和服务器模型权重没有整体迁入。
