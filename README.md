# Agent 看板（agent_board）

macOS 本机 AI Agent 集中监控看板 + 任务流水线。一个 Chrome 应用窗口，统一掌控
本机所有 AI agent 的运行状态、正在干什么、是否在等你审批，并能一键跳转、
一键派发多 agent 协作任务。

## 功能总览

### 1. 全家福状态监控
- 支持：**Claude Code、Codex CLI、Mimo Code、Hermes Agent、ZCode、
  DeepSeek Harness、Gemini CLI**，以及 ChatGPT / Claude 桌面版 / Cursor /
  Cherry Studio 等桌面应用；
- 每张卡片显示：彩色状态标签（等待审批 / 忙碌 / 运行中 / 空闲 / 未运行）、
  当前模型与版本、项目目录、最近会话、今日会话数、token 用量、代码增删统计、
  CPU / 内存 / 最后活动时间；
- 主界面按状态分三层：🔥 正在干活（忙碌 + 等待审批）→ ● 运行中 / 空闲 →
  ○ 未运行，后两层可折叠；状态经 SSE 每 2 秒实时刷新。

### 2. 等待审批检测（hook 协议，零 token 消耗）
- 通过各 agent 的 hook / 插件机制，审批框弹出时写本地标记文件、
  批准后清除——看板实时显示「等待审批」并提示**待批准的内容**
  （如"待批准：Bash"）；
- 纯本地文件操作，不经过模型、不产生任何 token 消耗；
- 看板侧自带孤儿标记复核：会话进程结束或审批后已有新动作时自动清理。

### 3. 会话级跳转
- 卡片内直接列出最近 3 个未隐藏会话，**点击即在新 iTerm2 标签
  cd 到项目目录并 resume 该会话**；
- 跳转"等待审批中"的会话时自动附加继续指令，让审批框在新标签重现；
- 会话弹窗支持隐藏 / 恢复 / 自定义改名，均持久化。

### 4. 任务流水线（多 agent 组群协作）
- 任务拆成多步，每步派给任意 CLI agent 的无头模式
  （`claude -p` / `codex exec` / `mimo run` / `gemini -p` / dsh headless）；
- 上一步输出经 `{input}` 占位符注入下一步提示词，逐步串联执行；
- 输出实时显示并落盘 `runs/`，支持取消与超时保护。

### 5. 系统通知
- agent 进入"等待审批"且看板不在前台时，弹 macOS 系统通知，不错过任何审批。

## 快速开始

```sh
uv sync                # 安装依赖（Python ≥ 3.13）
uv run workbench       # 启动服务（默认 :8765）并自动弹出看板窗口
uv run workbench open  # 服务已在运行时，随时弹出看板窗口
```

- 新增 agent：`src/agent_board/agents/` 下写一个 `probe(scan)` 并注册，
  或直接在界面「⚙ 管理」里添加（桌面应用 / CLI 进程 / Web 服务三种类型）；
- 管理界面支持隐藏 / 改名 / 编辑自定义 agent，配置存于工作区 `config.json`。

## 架构

```
src/agent_board/
  registry.py        # 探测器注册表 + snapshot()：并发探测出一帧状态（含等待审批、会话预览）
  agents/            # 各 agent 探测器：claude/codex/mimo/hermes/zcode/dsh/gemini/gui/custom
  web.py             # FastAPI：SSE 状态流、会话/跳转/管理/流水线 API（只绑 127.0.0.1）
  control.py         # 进入动作：iTerm2 新标签 resume / open -a / 浏览器
  tasks.py           # 流水线 runner：无头执行、{input} 串联、取消、超时
  config.py          # config.json：隐藏列表、改名、自定义 agent、隐藏会话、会话改名
  cli.py             # workbench 入口：起服务 + 自动弹窗；workbench open
  static/            # Vue 3 前端（vendor 本地，无构建步骤）
scripts/             # 审批标记 hook 脚本（协议见 AGENTS.md）
runs/                # 流水线任务输出日志
```

## 隐私与安全红线

- 探测器**只读元数据**（标题 / 目录 / 时间戳 / 模型），禁止读取或展示对话内容；
- `~/.codex/config.toml` 含明文 API token，绝不读取；
- 服务只绑 `127.0.0.1`；流水线会真实执行 CLI（消耗额度），仅由用户显式点击发起；
- `~/.zcode/v2/credentials.json`、`~/.dsh/.credentials.yaml` 等凭证文件不读。

更多实现细节（各 agent 数据源、hook 协议、已知坑）见 [AGENTS.md](AGENTS.md)。
