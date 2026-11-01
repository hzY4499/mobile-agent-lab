# 本机 Android harness 配置

此目录是在当前 Ubuntu 主机上的 UI-Venus-2 官方移动端 harness。手机通过本机 USB ADB 连接；模型请求默认发给 H20 上的 Qwen3.8-27B，沿用 Venus 的截图、动作解析和 ADB 执行链路。

## 已配置

- ADB：`/home/kojoker/.local/android/platform-tools/adb`
- 设备：`AVRUVB2519001424`（USB，当前分辨率 1080×2400）
- Python 环境：`.venv`
- 模型 API：`http://172.23.148.117:18082/v1`
- 模型名：`Qwen3.8-27B`
- 配置：`Venus_framework/Venus_framework_mobile/config/local_ui_venus_2.yaml`

## 运行任务

从仓库根目录执行：

```bash
./run_android_agent.sh \
  --purpose "打开系统设置" \
  --trace-dir record/traces \
  --step-limit 30
```

运行前请唤醒并解锁手机。脚本会自动把本机 ADB 放入 `PATH`，并使用隔离的 `.venv`。

未设置 `API_KEY`、`MODEL_API_KEY` 或 `OPENAI_API_KEY` 时，启动脚本通过现有 `ssh h20` 连接读取 `/data3/yhz/qwen38-deploy/api_key`，仅放入进程环境，配置文件中的 `api_key` 保持为空。

## ADB Keyboard 状态

官方 harness 的 `Type(...)` 和清空输入框依赖 ADB Keyboard（包名 `com.android.adbkeyboard`）。该 APK 已安装并启用，当前默认输入法为 `com.android.adbkeyboard/.AdbIME`；百度输入法仍保留在可用输入法列表中。APK 来源为项目 README 指定的 `senzhk/ADBKeyBoard` 仓库。

```bash
adb -s AVRUVB2519001424 shell ime enable com.android.adbkeyboard/.AdbIME
adb -s AVRUVB2519001424 shell ime set com.android.adbkeyboard/.AdbIME
```

已通过 Chrome 地址栏验证 `ADB_CLEAR_TEXT` 和 `ADB_INPUT_TEXT` 广播：文本 `ADBKeyboard_OK` 能写入当前焦点输入框。
