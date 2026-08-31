# Web E2E Test Studio

## Markdown business workflows

Business-facing forms can now persist their values into a `*.workflow.md`
document. The document combines an editable YAML `data` map with conditional
business stages and human-readable knowledge recovered from existing test
assets. Studio compiles it into a Playwright Authoring Run, then keeps the
existing deterministic `TestAsset -> plain pytest` export path.

```powershell
.\.venv\Scripts\web-e2e-studio.exe workflow compile examples\basic_navigation.workflow.md
.\.venv\Scripts\web-e2e-studio.exe workflow update examples\basic_navigation.workflow.md --set expected_heading="Example Domain"
.\.venv\Scripts\web-e2e-studio.exe workflow run examples\basic_navigation.workflow.md --output-dir recordings
```

Deployment URLs and passwords use `${ENV:NAME}` and are not stored as plaintext
in exported assets. See [the workflow format](docs/markdown-workflows.md).

Domain workflows, including the B2 contract workflow, are use-case assets under
`examples`; they are not product capabilities and are not imported by the
Studio runtime.

这是一个以 Python、PyTest 和 Playwright 为基础的多能力测试 Agent。它参考
Midscene Core 的 Agent 分层与执行闭环，并结合本地 `vlm-studio` 的产品目标：

- 在真实浏览器中手工操作并录制；
- 让 VLM Agent 观察页面、规划并执行下一步；
- 注册并录制本地 Python、PowerShell、Node 或可执行工具；
- 只把执行成功且验证通过的动作写入测试资产；
- 导出不需要模型、可独立运行的纯 PyTest 文件；
- 通过现代 Code Agent 风格的 Desktop Studio 或命令行工作。
- 将浏览器、HTTP API 和项目外部工具作为可发现、可扩展的能力接入。

## 安装

要求 Python 3.11 或更高版本。在 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 使用 Desktop Studio

启动桌面端：

```powershell
.\.venv\Scripts\web-e2e-desktop.exe
```

也可以指定允许外部工具运行的工作区：

```powershell
.\.venv\Scripts\web-e2e-desktop.exe --workspace "C:\your-project"
```

Desktop Studio 以 Agent 对话为中心：左侧管理项目和会话，中间是任务对话与
Agent 工具调用流，右侧展示浏览器观察、已验证时间线、产物和动态能力目录。

推荐使用顺序：

1. 填写 `Start URL`、测试名称、目标和输出目录。
2. 点击 `Start browser`，Studio 会打开 Chromium 并录制初始导航。
3. 在底部输入框中直接描述完整任务；Agent 可以组合 Playwright 浏览器动作、
   `http_request` API 动作和已注册的项目工具。
4. 需要人工接管时，直接操作 Chromium，再点击 `同步手工操作`。
5. 在右侧 `能力` 页签注册 Python、PowerShell、Node 或可执行工具；能力列表
   由后端适配器动态提供，不写死在界面中。
6. 点击 `Save & export`。输出目录中会同时生成：
   - `<name>.json`：版本化、可审计的录制资产；
   - `test_<name>.py`：不依赖 VLM 的纯 PyTest 文件。
7. 完成后点击 `Stop` 关闭浏览器。

工作区通过 `--workspace` 或启动目录决定，任务名称、URL 和输出目录由每个会话
单独配置，不依赖机器上的固定绝对路径。

## 配置 VLM

只有录制阶段的 VLM 辅助功能需要模型。启动 Desktop Studio 或 CLI 前设置：

```powershell
$env:VLM_API_KEY = "..."
$env:VLM_MODEL = "gpt-4o"
# 使用兼容 OpenAI API 的其他服务时设置：
$env:VLM_BASE_URL = "https://your-provider.example/v1"
```

也兼容 `OPENAI_API_KEY` 与 `OPENAI_BASE_URL`。导出的 PyTest 不会读取这些变量，
也不会导入 `openai` 或本项目运行时代码。

Desktop 启动时会自动读取 `--workspace` 指向目录中的 `.env`，已存在的系统环境
变量优先。也可以点击顶部 `Model settings` 在当前 Desktop 会话中配置或覆盖
API Key、模型名和 Base URL；API Key 不会写入录制资产或导出的 PyTest。
如果内网 VLM 不应经过系统代理，可勾选 `Ignore system proxy`，或在 `.env`
设置 `VLM_IGNORE_PROXY=true`。

## 使用命令行录制

```powershell
.\.venv\Scripts\web-e2e-studio.exe record `
  --url "https://example.com" `
  --goal "搜索 invoice 123 并打开详情" `
  --name "invoice_search" `
  --asset "artifacts/invoice_search.json" `
  --output "generated/test_invoice_search.py"
```

浏览器启动后，在终端中使用：

```text
<Enter> / :sync       捕获尚未同步的手工浏览器操作
:ai <instruction>     让 VLM 观察、执行、验证并录制
:tool <id> [args...]  执行并录制已注册的外部工具
:save                 保存资产并重新生成 PyTest
:help                 查看帮助
:quit                 同步、保存、导出并关闭浏览器
```

启动时注册外部工具：

```powershell
.\.venv\Scripts\web-e2e-studio.exe record `
  --url "https://example.com" `
  --goal "打开页面并运行数据准备工具" `
  --tool ".\examples\tools\echo_tool.py"
```

外部工具会被复制到工作区内的受控目录，以参数数组和 `shell=False` 执行。
当前版本录制的是外部进程调用；若要捕获工具内部的桌面鼠标/键盘细节，可以继续增加
pywinauto 或 WinAppDriver 类型的 Desktop Adapter。

## 重新导出和运行

```powershell
.\.venv\Scripts\web-e2e-studio.exe export `
  artifacts/invoice_search.json `
  --output generated/test_invoice_search.py

.\.venv\Scripts\python.exe -m pytest generated/test_invoice_search.py -q
```

检查或校验资产：

```powershell
.\.venv\Scripts\web-e2e-studio.exe inspect artifacts/invoice_search.json
.\.venv\Scripts\web-e2e-studio.exe validate artifacts/invoice_search.json
```

## 核心设计

```text
手工事件 / 用户指令 / VLM 决策
                 |
           StepCandidate
                 |
     ActionRegistry 校验与执行
          /                 \
 PlaywrightDevice     ExternalToolExecutor
          \                 /
          重新观察并确定性验证
                 |
       仅 verified 动作进入资产
                 |
              TestAsset
                 |
          PytestExporter
                 |
       无模型依赖的 test_*.py
```

详细说明见 [架构文档](docs/architecture.md)、[规格说明](docs/spec.md) 和
[项目上下文](CONTEXT.md)。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\ruff.exe check src tests
```

真实浏览器测试：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.playwright-browsers"
$env:RUN_BROWSER_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -m browser
```
