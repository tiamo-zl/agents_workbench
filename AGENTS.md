# AGENTS.md — agents_workbench

## What this is

**Agent 看板（agent_board）**：本机 AI Agent 集中监控看板 + 任务流水线，跑在 macOS
上。通过只读本地数据源展示 claude code / codex / ZCode / deepseek-harness /
gemini / 各 GUI 应用的运行状态与"正在干嘛"，支持一键进入对应 agent，并能把任务
以无头模式派给 CLI agent、按步骤串联成流水线。

## Environment

- Python **>= 3.13**，uv 管理（`uv sync` / `uv add` / `uv run`）。
- 前端：Vue 3 全局构建，**已 vendor 到 `src/agent_board/static/vendor/`**，无
  CDN 依赖、无 npm 构建步骤；改前端只需编辑 `static/` 后刷新页面。
- 依赖：fastapi + uvicorn（SSE 用）、psutil。
- macOS 专用：依赖 AppleScript（激活 iTerm2/应用）；服务只绑 127.0.0.1。

## Commands

```sh
uv run workbench                 # 起服务(默认 :8765) + 自动弹出 Chrome app 看板窗口
uv run workbench open            # 不起服务，随时弹出看板窗口（服务需已在运行）
uv run workbench --no-browser    # 只起服务
uv run workbench --port 8900     # 换端口
```

无测试/lint 配置；加了要在这里记录。

## Architecture

```
src/agent_board/
  registry.py    # 探测器注册表 + snapshot()：并发跑探测器出一帧数据
  config.py      # config.json（工作区根）：hidden 隐藏列表 + custom 自定义 agent
  agents/base.py # AgentStatus 模型 + ProcScan(进程快照,一次扫描多探测器复用)
  agents/*.py    # claude/codex/zcode/dsh/gemini 各自的 probe(scan)；gui.py 数据驱动
  agents/custom.py # 自定义探测器（app/cli/web 三类），由 config.json 驱动
  web.py         # FastAPI：/ 看板、/api/status(SSE 2s)、/api/open/{id}、/api/tasks、
                 #   /api/manage（列表/隐藏/改名/自定义增删改）、
                 #   /api/sessions/{id}(列表含hidden)+hide + /api/open/{id}/session（会话跳转）
  control.py     # 进入动作：AppleScript 激活 iTerm 匹配窗口 / open -a / open URL /
                 #   open_in_iterm（新标签 cd+resume）
  tasks.py       # 流水线 runner：headless 跑 CLI、{input} 串上一步输出、可取消
  cli.py         # workbench 入口：起 uvicorn + Chrome --app 窗口
  static/        # Vue 前端(index.html 内联模板 + app.js + style.css)
runs/            # 每个流水线任务的步骤输出日志
config.json      # 看板配置（用户通过管理界面维护）：hidden 隐藏 agent、
                 #   names 显示名覆盖、hidden_sessions 隐藏的会话（按 agent）、
                 #   session_names 会话自定义显示名（覆盖原标题，仅本地）、
                 #   custom 自定义 agent
```

- 新增一个 agent：在 `agents/` 写 `probe(scan) -> AgentStatus`，注册进
  `registry.PROBES`，`ORDER` 里补 id；GUI 应用只需在 `agents/gui.py` 的 `APPS`
  加一行。可派任务的 agent 需同时改 `registry.TASK_AGENTS` 和
  `tasks._cmd()`。**用户侧**不需要改代码：看板「⚙ 管理」界面即可隐藏/恢复
  内置 agent、添加自定义 agent（app/cli/web 三类，存 config.json）。
- 探测器必须**容错**：单探测器异常被 registry 捕获降级，不许拖垮整帧；
  probe 函数挂 `_agent_id` 属性以便降级卡片可定位。
- 每帧快照会全量扫描进程（`ProcScan` 一次），探测器之间不要重复扫。

## 各 agent 状态数据源（已验证）

- claude code：`~/.claude/sessions/<pid>.json`（status/cwd/name，epoch 毫秒）
  ——pid 已死的文件要跳过。会话列表读 `~/.claude/projects/*/<uuid>.jsonl`
  的**元数据行**：cwd 在每行、标题在 `type:"ai-title"` 行的字段 `aiTitle`
  （目录名反解有歧义，cwd 必须从行内取）；只 tail/head 读文件片段。
- codex：`~/.codex/session_index.jsonl`（每行 {id,thread_name,updated_at}）+
  `thread-writer-locks/*.lock` 与当天 `sessions/YYYY/MM/DD/*.jsonl` mtime 判活跃。
  会话 cwd 取 `sessions/YYYY/MM/DD/rollout-*-<id>.jsonl` 首行
  `session_meta.payload.cwd`。
- gemini：会话走 `gemini --list-sessions` / `--resume <序号>`（按项目生效，
  看板固定在 ~；序号会漂移）。`~/.gemini/tmp/*/logs.json` 含对话内容，**不读**。
- mimo（mimocode，opencode 系）：会话在 `~/.local/share/mimocode/mimocode.db`
  （只读）session 表，directory 字段即 cwd；进程按参数精确匹配
  （`mimo` / `*/mimo` / 含 mimocode）。恢复 `mimo -s <id>`；无头任务 `mimo run`。
- hermes：进程 = 命令行含 hermes；会话解析 `hermes sessions list` 表格输出
  （Title/Preview/Last Active/ID，ID 形如 20260905_230913_1b6d25），不读
  sessions/*.json 里的 messages；恢复 `hermes --resume <ID>`（它自己还原 cwd）。
- dsh：会话 = `~/.dsh/sessions/<工作区slug>/session-<uuid>/`（内容 zstd 压缩，
  不读）；slug 用贪心 + isdir 还原成路径，已删除的工作区回落 ~。web profile
  **不支持** `--resume`、前端无深链 → 会话进入 = 浏览器打开 Web UI（cookie 鉴
  权在用户浏览器里），界面内选会话。
- cursor：最近工作区在 `~/Library/Application Support/Cursor/User/globalStorage/
  state.vscdb`（只读）的 `history.recentlyOpenedPathsList`，id = 目录路径，
  进入 = `open -a Cursor <路径>`（不经 iTerm2）；磁盘上已删除的目录要过滤掉。
- 会话进入按形态分流（web.py）：cli → `SESSION_COMMANDS`（iTerm2 + 恢复命令，
  id 校验 `[A-Za-z0-9_-]{4,64}`）；app → `SESSION_APP_OPENERS`（直开应用，sid
  须为存在的绝对路径）；web → `SESSION_URL_OPENERS`（浏览器开 URL）。
- ZCode：`~/.zcode/cli/db/db.sqlite` session 表，**必须 `file:...?mode=ro`
  只读打开**（应用持有写锁）；time_updated 是 epoch 毫秒。
- deepseek-harness：进程 + `127.0.0.1:3080` 探活；HTTP API 有 launch-token 鉴权
  （401），别调；活动时间取 `~/.dsh/sessions/` mtime。

## 「等待审批」hook 机制（协议 v2）

- 标记目录 `~/.agent_board/approvals/`：
  - `approvals/<agent>/<session_id>` 文件 = 该会话在等待（hook 载荷带
    session_id 时，会话级）；
  - `approvals/<agent>` 平文件 = agent 级等待（hook 拿不到会话 id 的兼容
    形态；注意会话目录存在时会被 is_dir 分支遮蔽，两者不混用）。
- 脚本：`scripts/approval-hook <agent> <sid|-> request|resolve`（agent 级
  resolve 用 rm -rf 以兼容遗留目录）；`scripts/approval-hook-claude <event>`
  解析 stdin JSON 提取 session_id/message。
- registry.snapshot()：非 offline 卡片命中标记 → `state=waiting` +
  activity「等待审批确认（N 个会话）」+ 卡片带 `waiting_sessions: [sid…]`
  随 SSE 下发（会话弹窗实时跟随打徽标）；mtime 超 1h 视为残留。
- claude 已接线（settings.json hook，备份 *.bak-*）：`Notification`
  （消息含 permission → request，否则 resolve）+ `PostToolUse` /
  `UserPromptSubmit` / `Stop` → resolve。全部零 token（本地文件操作，
  hook 输出不进上下文）。
- 跳转等待审批中的会话（`SESSION_CONTINUE_NUDGE`）：resume **不会**恢复旧
  进程挂起的审批请求，跳转命令自动附加 "continue" 初始提示让 agent 恢复后
  立即继续、审批框在新标签重现（仅当该 sid 有等待标记时）。
- mimo：**已接线**，插件 `~/.mimocode/plugin/agent-board-approval.js`
  （mimo 从 ~/.mimocode/plugin/ 加载）：`permission.ask`（载荷含 sessionID）→
  request，`tool.execute.before` → resolve；拒绝场景靠 1h 过期兜底。
- hermes：**已接线**，config.yaml 尾部 `hooks:` 块（备份 *.bak-*）：
  `pre_approval_request` → request，`post_approval_response` / `on_session_end`
  → resolve；首次触发会在 hermes 会话内弹一次授权确认（shell-hooks
  allowlist 机制）。
- codex：**无事件源**——notify 只有 agent-turn-complete、rollout 不落审批
  事件（审批走 TUI 协议流），本地拿不到审批信号，暂只有 idle/busy。

## Privacy / 安全红线

- 探测器只读**元数据**（title/cwd/timestamp/model），禁止读取或展示对话内容。
- `~/.codex/config.toml` 含明文 API token，**绝不读取**。
- `~/.zcode/v2/credentials.json`、`~/.dsh/.credentials.yaml` 不读。
- 服务只绑 127.0.0.1；任务 runner 会真实执行 CLI（花用户额度），只能由用户
  在看板上显式点击发起。

## Known gotchas

- AppleScript 首次运行会弹 macOS 自动化授权（iTerm2 / System Events），拒绝后
  进入动作退化为"仅激活 iTerm2"。
- psutil `cpu_percent(None)` 差值语义：每帧第一遍返回 0，第二帧起才准。
- 时间戳各 agent 不统一：claude/ZCode 落盘 epoch 毫秒，用 `base.ms_to_ts()` 归一。
- dsh 的 `--profile headless`、codex 的 `exec --skip-git-repo-check` 用于无头
  派任务；claude 用 `-p`。
