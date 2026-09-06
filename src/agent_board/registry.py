"""Agent 注册表与状态快照。

内置 agent：在 agents/ 下写一个 probe(scan) -> AgentStatus | list[AgentStatus]，
加进 PROBES 即可（GUI 应用只需在 gui.APPS 里加一行）。
用户自定义 agent：看板「管理」界面添加，存 config.json，由 agents/custom.py 数据驱动。
"""
from __future__ import annotations

import glob
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as board_config
from .agents import base, claude, codex, custom, dsh, gemini, gui, hermes, mimo, zcode
from .agents.base import AgentStatus, ProcScan

PROBES = [
    claude.probe,
    codex.probe,
    mimo.probe,
    hermes.probe,
    zcode.probe,
    dsh.probe,
    gemini.probe,
    *gui.make_probes(),
]

# 流水线可派任务的 agent（tasks.AGENT_COMMANDS 的键需与之对应）
TASK_AGENTS = ["claude", "codex", "mimo", "gemini", "dsh"]

# 固定展示顺序：核心 CLI/服务在前，桌面应用在后，自定义排在最后
ORDER = ["claude", "codex", "mimo", "hermes", "zcode", "dsh", "gemini",
         "chatgpt", "claude-desktop", "cursor", "cherry-studio"]

# 「核心 Agent」区的成员（其余进「桌面应用」区）；看板分组由后端下发，
# 前端不做判断，新增 agent 只改这里
CORE_IDS = {"claude", "codex", "mimo", "hermes", "zcode", "dsh", "gemini"}

# 会话预览 provider（卡片「最近 5 个会话」行）；web.py 的跳转映射也引用此表
SESSION_PROVIDERS = {
    "claude": claude.list_sessions,
    "codex": codex.list_sessions,
    "mimo": mimo.list_sessions,
    "hermes": hermes.list_sessions,
    "gemini": gemini.list_sessions,
    "dsh": dsh.list_sessions,
    "cursor": gui.list_cursor_sessions,
}
# 预览缓存：30s TTL，避免每 2s 的快照重复扫会话文件
_preview_cache: dict = {}


def _sessions_preview(agent_id: str, hidden_ids: frozenset, limit: int = 3) -> list:
    provider = SESSION_PROVIDERS.get(agent_id)
    if provider is None:
        return []
    ent = _preview_cache.get(agent_id)
    if ent and time.time() - ent["ts"] < 30:
        return ent["items"]
    items = ent["items"] if ent else []
    try:
        res = provider(limit, hidden_ids)
        items = res.get("visible", [])[:limit]
        _preview_cache[agent_id] = {"ts": time.time(), "items": items}
    except Exception:
        pass   # 拉取失败沿用旧预览
    return items


# 「等待审批」标记目录（协议 v2）：
#   approvals/<agent>/<session_id>  文件 = 该会话在等待（hook 拿到会话 id）
#   approvals/<agent>               平文件 = agent 级等待（兼容形态）
# scripts/approval-hook 提供 touch/rm；mtime 超过 APPROVAL_MAX_AGE 视为残留。
APPROVAL_DIR = Path.home() / ".agent_board" / "approvals"
APPROVAL_MAX_AGE = 3600  # 秒


def approval_markers(agent_id: str) -> tuple[bool, dict]:
    """返回 (agent 级等待, {session_id: 审批消息})。

    标记文件可能是空文件（旧协议/其它 agent 的 hook）或
    {"msg": ..., "ts": ...} JSON（claude 新协议，带审批内容）。
    """
    base = APPROVAL_DIR / agent_id
    now = time.time()
    waiting: dict = {}
    try:
        if base.is_dir():
            for p in base.iterdir():
                try:
                    if not p.is_file() or now - p.stat().st_mtime >= APPROVAL_MAX_AGE:
                        continue
                    msg = ""
                    try:
                        d = json.loads(p.read_text(encoding="utf-8"))
                        msg = (d.get("msg") or "").strip()
                    except (ValueError, OSError):
                        pass
                    waiting[p.name] = msg
                except OSError:
                    continue
            return False, waiting
        if base.is_file():
            return (now - base.stat().st_mtime) < APPROVAL_MAX_AGE, waiting
    except OSError:
        pass
    return False, waiting

def _claude_waiting_filter(waiting: dict) -> dict:
    """claude 标记复核，清掉孤儿标记：
    - 会话进程已结束 → 审批框必然已消失；
    - 标记写入之后转录（projects/<sid>.jsonl）又有新写入 → 审批已被处理。
    """
    out = {}
    for sid, msg in waiting.items():
        marker = APPROVAL_DIR / "claude" / sid
        try:
            marker_mtime = marker.stat().st_mtime
        except OSError:
            continue
        if sid not in claude.live_session_ids():
            marker.unlink(missing_ok=True)
            continue
        tp = glob.glob(os.path.join(str(Path.home() / ".claude" / "projects"),
                                    "*", sid + ".jsonl"))
        newest = max((os.path.getmtime(p) for p in tp if os.path.exists(p)), default=0)
        if newest > marker_mtime:
            marker.unlink(missing_ok=True)
            continue
        out[sid] = msg
    return out


# 各 agent 可选的等待标记复核器
WAITING_FILTERS = {"claude": _claude_waiting_filter}

# 管理界面用的内置 agent 元数据（与 PROBES 保持同步）
BUILTIN_META = [
    {"id": "claude", "name": "Claude Code", "kind": "cli", "desc": "实时状态来自 ~/.claude/sessions"},
    {"id": "codex", "name": "Codex CLI", "kind": "cli", "desc": "会话索引 + 写入锁判活跃"},
    {"id": "mimo", "name": "Mimo Code", "kind": "cli", "desc": "mimocode.db（只读）判活跃与会话"},
    {"id": "hermes", "name": "Hermes Agent", "kind": "cli", "desc": "进程 + sessions 目录判活跃"},
    {"id": "zcode", "name": "ZCode", "kind": "app", "desc": "只读本地 SQLite 会话库"},
    {"id": "dsh", "name": "DeepSeek Harness", "kind": "web", "desc": "127.0.0.1:3080 探活"},
    {"id": "gemini", "name": "Gemini CLI", "kind": "cli", "desc": "进程 + ~/.gemini/tmp 活动"},
    *[{"id": a["id"], "name": a["name"], "kind": "app", "desc": "桌面应用"} for a in gui.APPS],
]


def snapshot() -> dict:
    """并发跑全部探测器，返回看板一帧数据（同步函数，供线程池调用）。"""
    cfg = board_config.load()
    probes = list(PROBES) + custom.make_probes(cfg["custom"])
    scan = ProcScan()

    def run(probe_fn):
        try:
            result = probe_fn(scan)
        except Exception as exc:  # 单个探测器故障不拖垮整块看板
            fid = getattr(probe_fn, "_agent_id", None) or f"error-{probe_fn.__module__.rsplit('.', 1)[-1]}"
            result = AgentStatus(id=fid, name=fid, kind="app", state="offline",
                                 activity=f"探测器异常: {exc}")
        if isinstance(result, AgentStatus):
            result = [result]
        return [s.to_dict() for s in result]

    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        lists = list(pool.map(run, probes))

    hidden = set(cfg["hidden"])
    names = cfg.get("names", {})
    hidden_sessions_cfg = cfg.get("hidden_sessions", {})
    cards = [c for lst in lists for c in lst if c["id"] not in hidden]
    for c in cards:
        c["name"] = names.get(c["id"], c["name"])   # 用户改过的显示名优先
        c["core"] = c["id"] in CORE_IDS
        c["waiting_sessions"] = []
        c["waiting_msgs"] = {}
        c["sessions_preview"] = _sessions_preview(
            c["id"], frozenset(hidden_sessions_cfg.get(c["id"], []))
        ) if c["id"] in SESSION_PROVIDERS else []
        if c["state"] != "offline":
            agent_waiting, waiting_map = approval_markers(c["id"])
            flt = WAITING_FILTERS.get(c["id"])
            if flt and waiting_map:
                waiting_map = flt(waiting_map)
            if agent_waiting:
                c["state"] = "waiting"
                c["activity"] = "等待审批确认"
            elif waiting_map:
                c["state"] = "waiting"
                c["activity"] = f"等待审批确认（{len(waiting_map)} 个会话）"
            c["waiting_sessions"] = list(waiting_map)
            c["waiting_msgs"] = waiting_map
    custom_kinds = {c["id"]: c["type"] for c in cfg["custom"]}
    for c in cards:   # 自定义 agent 按类型归区：app 进桌面应用，cli/web 进核心
        if c["id"] in custom_kinds:
            c["core"] = custom_kinds[c["id"]] != "app"
    cards.sort(key=lambda c: ORDER.index(c["id"]) if c["id"] in ORDER else 99)
    return {"ts": time.time(), "agents": cards}
