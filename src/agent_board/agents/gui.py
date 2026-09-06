"""桌面 GUI 应用探测器（数据驱动，新增应用改 APPS 即可）。

GUI 应用没有可靠的结构化活动数据，V1 只提供：运行状态 + CPU/内存 + 一键打开。
例外：Cursor 的最近工作区存在本地 sqlite（只读），支持「会话」式打开。
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse

from .base import AgentStatus, ProcScan

# match: 进程 exe/命令行中的特征串（对应 /Applications/<名>.app）
# has_sessions: 该应用支持「会话」列表（当前只有 Cursor）
APPS = [
    {"id": "chatgpt", "name": "ChatGPT", "app": "ChatGPT", "match": "ChatGPT.app"},
    {"id": "claude-desktop", "name": "Claude 桌面版", "app": "Claude", "match": "Claude.app"},
    {"id": "cursor", "name": "Cursor", "app": "Cursor", "match": "Cursor.app", "has_sessions": True},
    {"id": "cherry-studio", "name": "Cherry Studio", "app": "Cherry Studio", "match": "Cherry Studio.app"},
]

CURSOR_STATE_DB = os.path.expanduser(
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")


def list_cursor_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    """Cursor 最近打开的工作区（state.vscdb 的 recentlyOpenedPathsList，只读）。

    会话 id = 工作区目录路径；进入动作 = `open -a Cursor <路径>`。
    """
    items = []
    try:
        conn = sqlite3.connect(f"file:{CURSOR_STATE_DB}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key='history.recentlyOpenedPathsList'"
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            for e in json.loads(row[0]).get("entries", []):
                uri = e.get("folderUri") or e.get("fileUri") or ""
                if not uri.startswith("file://"):
                    continue
                path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
                if path and os.path.isdir(path):
                    items.append({"id": path, "title": os.path.basename(path) or path,
                                  "cwd": path, "last_active": None, "running": False})
    except (sqlite3.Error, OSError, ValueError):
        pass
    visible = [x for x in items if x["id"] not in hidden_ids][:limit]
    hidden = [x for x in items if x["id"] in hidden_ids][:50]
    return {"visible": visible, "hidden": hidden}


def make_probes():
    def _make(cfg: dict):
        def probe(scan: ProcScan) -> AgentStatus:
            hs = bool(cfg.get("has_sessions"))
            procs = scan.with_exe_path(cfg["match"])
            if not procs:
                return AgentStatus(
                    id=cfg["id"], name=cfg["name"], kind="app", state="offline",
                    activity="未运行",
                    enter={"type": "app", "app": cfg["app"]},
                    has_sessions=hs,
                )
            cpu, mem = scan.usage(procs)
            return AgentStatus(
                id=cfg["id"], name=cfg["name"], kind="app", state="running",
                activity="运行中",
                pid=procs[0].info["pid"],
                cpu_percent=cpu or None,
                mem_mb=mem or None,
                enter={"type": "app", "app": cfg["app"]},
                has_sessions=hs,
            )
        probe._agent_id = cfg["id"]   # 降级时能定位到具体 agent
        return probe

    return [_make(cfg) for cfg in APPS]
