# GUI测试集v1：一条命令运行

固定程序负责准备页面、调用Qwen + 原生Venus、记录录像和请求、汇总表格。不调用Codex或外部付费判分模型。服务继续使用H20现有vLLM容器；没有修改模型权重/模板、重新安装环境或改动原始实验结果。

## 开始

连接测试手机、手动解锁、确保微信已登录。其余页面由程序自动准备。

```bash
cd /home/kojoker/projects/UI-Venus
./run_gui_benchmark.sh run --model qwen38
```

默认A01～A15、E01～E05，各3次，共60次。向已授权的Y_Yao发送测试内容。启动时自动切换H20对应现有容器并等待就绪；忙碌服务由现有管理脚本拒绝停止。输出目录自动创建在桌面`HuaWei/测试结果/自动benchmark`，终端打印完整路径。

```bash
# 4B
./run_gui_benchmark.sh run --model qwen35
# 顺序运行4B、27B，每组60次
./run_gui_benchmark.sh run --model both
# 先试一条，不发消息
./run_gui_benchmark.sh run --model qwen38 --cases A01 --repeats 1
# 可修改温度；本次实际请求使用0.7
./run_gui_benchmark.sh run --model qwen38 --temperature 0.7
# 固定输出目录（必须不存在，避免覆盖旧数据）
./run_gui_benchmark.sh run --model qwen38 --output /home/kojoker/Desktop/HuaWei/测试结果/我的新实验
```

省略`run`也可直接`./run_gui_benchmark.sh --model qwen38`。

## 自己修改配置

[config.yaml](/home/kojoker/projects/UI-Venus/benchmarks/gui_v1/config.yaml)包含温度、采样、max_tokens、重复次数、原子/端到端步数和超时。可复制该文件，用`--config /绝对路径/my.yaml`运行。`--temperature`和`--repeats`优先于配置文件。

两个模型默认温度均为1；top_p=.95、top_k=20、min_p=0、repetition=1、frequency=0、seed=0、max_tokens=16384一致。presence_penalty遵循之前核查的各自推荐设置：4B为1.5，27B为0。官方架构、Chat Template不改。它们都是可见的实验参数，不会默默恢复旧27B的0.6。

程序将配置冻结在每个结果目录的`manifest.json`和`frozen-config.yaml`。**恢复实验使用冻结配置**，不会读取你后来改过的温度。改温度请创建新实验。每一轮保存真正发送的`api-NNN-request.json`，并校验请求参数和返回模型；API密钥从环境变量或SSH私下读取，不写入配置或命令行。

`api-NNN-request.json`记录每次SDK调用；`http-NNN-request.json`记录序列化后发出的HTTP请求体，extra_body已合并为顶层字段，SDK内部重试也分别保存。两层请求都校验冻结参数，HTTP响应另存对应文件；请求头中的密钥不会记录。

## 自动准备与边界

适配当前Honor手机AVRUVB2519001424、1080×2400、微信Y_Yao、荣耀浏览器、搜索词天竺葵。自动操作：回到微信主页/桌面、搜索联系人、进入私聊、清空或填写草稿、打开媒体选择页、勾选当前首图/唯一视频、准备浏览器搜索页/预检结果/正文。每个关键页面检查截图区域或UI节点，失败保存证据并暂停，不把错误初态计作模型失败。模型看不到准备脚本的坐标或模板。

手机供电时保持亮屏；每轮准备检查连接和解锁状态。锁屏需本人手动解锁。页面模板适用于目前版本/布局；系统字体、分辨率、联系人名称或App布局变化可能需要校准。程序不修改原图库，视频要求仍为唯一20～30秒素材，图片使用当前首图并记录媒体清单。

聊天准备会核验系统键盘状态：若上一轮留下已展开的键盘，直接清空当前输入框；只有键盘仍展开时才按返回收起。无法确认输入焦点或键盘状态时保留现场暂停，避免按返回误退出私聊。

**聊天记录默认保留，不自动删除。** 自动删除需要可靠辨认刚发送的具体消息，当前版本不盲点删除按钮。因此与之前“清理旧消息后”的六轮复测条件不同；新旧消息是否混淆需按本轮操作和前后截图判分。两模型串行运行也会有聊天历史、网页和缓存状态差异；统一温度不代表所有环境因素都完全相同。

每个模型启动/恢复后做一次相同的64-token预热（独立记录，不计GUI时间），保留Prefix Cache，不做后台清缓存。检查vLLM实际模型、KV块分配、Prefix状态；正式运行约1Hz记录KV占用和其他服务指标。调度保持串行。

## 停止与继续

终端按Ctrl+C，或另一个终端执行：

```bash
./run_gui_benchmark.sh stop /完整结果目录
./run_gui_benchmark.sh status /完整结果目录
./run_gui_benchmark.sh resume /完整结果目录
```

准备失败且未下发任务：恢复手机后`resume`重新准备。已经完成的PASS、FAIL、待核验轮次均跳过。正式执行中的断线或中止：保留INTERRUPTED，不自动重发可能已发送的消息；先在复核页检查，确认环境故障后标记ENVIRONMENT_INVALID，再执行：

```bash
./run_gui_benchmark.sh resume /完整结果目录 --retry-invalid
```

环境无效原记录归档后重试，模型错误不能标环境无效来抹去失败。停脚本不会关闭H20模型容器。`--no-switch`仅核验当前服务，不切换；模型不一致会暂停。

## 集中复核，不用实时守着

```bash
./run_gui_benchmark.sh review /完整结果目录
```

终端打印本机浏览器地址。一次查看所有轮次的初末截图、录像、逐步响应和动作，填写原因并保存。涉及接收端的用例，在同一页完成本轮收件/媒体打开/播放/链接正文确认。复核页仅监听127.0.0.1，有随机访问令牌，不使用外部API。浏览器访问日志不记录令牌。

自动判分：明确超时、步数耗尽、请求人工帮助或全程动作格式错误记FAIL；A01/A02/A03/A04/A07可使用校准页面规则确认PASS。其他成功声明默认REVIEW，端到端发送侧通过但未确认收件标PENDING_RECEIVER。**不会把“模型说完成”变成成功率。** 若只跑实验取日志，无需实时人工；若要严谨的完整成功率，仍需结束后集中复核媒体/新消息和接收端。

每次保存复核都会重新生成`REPORT.md`、`ledger.csv`和`analysis.json`，保留`review-history.json`修改历史。待定项与环境无效/中断均单列，不假定通过或失败。

## 输出

```text
结果目录/
  manifest.json, frozen-config.yaml   冻结配置/代码指纹
  REPORT.md, ledger.csv, analysis.json
  execution.json, status.json, review-queue.json
  qwen38/或qwen35/
    server-info.json, server-models.json, server-version.json
    warmup-request.json, warmup-response.json
    preparation/A01-r1/              准备检查/初始页面证据
    runs/A01-r1/
      summary.json, assessment.json, framework.log
      api-001-request.json, api-001-response.json
      http-001-request.json, http-001-response.json
      observation-001.png, initial.png, final.png
      screen-01.mp4, metrics.jsonl, traces/
```

请求的图片数据以SHA-256替代以节省重复存储，真实图像单独保存。录像720×1600，模型截图1080×2400；静态尾帧可能被编码器省略。GUI动作计数沿用原规则：双击计2，失败执行尝试计数，None、完成声明、截图、等待及准备不计数。

## 检查与测试

```bash
# 仅验证自动准备；不调用模型，不点击发送
./run_gui_benchmark.sh prepare-check --cases A01,A04,A09,A12,A15
./.venv/bin/python -m unittest discover -s benchmarks/gui_v1/tests -v
```

实际验证结果另见VALIDATION.md。新增程序位于benchmarks/gui_v1，不覆盖已有run_android_agent.sh或全局local_ui_venus_2.yaml。
