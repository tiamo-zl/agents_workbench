"""Hermes Agent CLI 探测器（python，~/.hermes）。

数据源：
- 进程：命令行含 hermes（/Users/lll/.local/bin/hermes，venv python 启动）。
- 会话：`hermes sessions list` 的表格输出（Title/Preview/Last Active/ID）——
  用工具自己展示的元数据，不解析会话 JSON 里的 messages 内容。
- 最近活动：~/.hermes/sessions/ 下最新 session_*.json 的 mtime。
恢复：`hermes --resume <ID>`（hermes 恢复时会自己还原工作目录，cwd 给 ~ 即可）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .base import AgentStatus, ProcScan, today_start

HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
SESSIONS_DIR = HERMES_HOME / "sessions"
RECENT_WINDOW = 120
SID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")


def _match_procs(scan: ProcScan):
    out = []
    for p in scan.procs:
        args = [a for a in (p.info.get("cmdline") or []) if a]
        if any("hermes" in a.lower() for a in args):
            out.append(p)
    return out


def _newest_session_mtime() -> float | None:
    newest = None
    if SESSIONS_DIR.is_dir():
        for p in SESSIONS_DIR.glob("session_*.json"):
            try:
                m = p.stat().st_mtime
                newest = m if newest is None or m > newest else newest
            except OSError:
                continue
    return newest


def _parse_rel(s: str) -> float | None:
    """"25m ago" / "3h ago" / "2026-04-16" → epoch 秒（近似）。"""
    m = re.match(r"(\d+)\s*([smhd])\b", s or "")
    if m:
        delta = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return time.time() - int(m.group(1)) * delta
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def list_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    bin_path = shutil.which("hermes") or str(HERMES_HOME / "bin" / "hermes")
    try:
        r = subprocess.run([bin_path, "sessions", "list"], capture_output=True,
                           text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {"visible": [], "hidden": []}

    visible, hidden = [], []
    for line in (r.stdout or "").splitlines():
        tokens = [t for t in re.split(r"\s{2,}", line.strip()) if t]
        if len(tokens) < 4 or not SID_RE.fullmatch(tokens[-1]):
            continue
        sid = tokens[-1]
        title, preview = tokens[0], tokens[1]
        item = {"id": sid,
                "title": title if title not in ("—", "-", "") else (preview or None),
                "cwd": os.path.expanduser("~"),
                "last_active": _parse_rel(tokens[-2]), "running": False}
        if sid in hidden_ids:
            if len(hidden) < 50:
                hidden.append(item)
        else:
            visible.append(item)
        if len(visible) >= limit and len(hidden) >= 50:
            break
    return {"visible": visible[:limit], "hidden": hidden}


_STATS = {"ts": 0.0, "data": {}}


def collect_stats() -> dict:
    """总/今日会话数与最近会话的消息数（15s 缓存，只读元数据头部）。"""
    now = time.time()
    if now - _STATS["ts"] < 15:
        return _STATS["data"]
    data = {}
    files = list(SESSIONS_DIR.glob("session_*.json")) if SESSIONS_DIR.is_dir() else []
    data["total_sessions"] = len(files)
    today = today_start()
    data["today_sessions"] = sum(1 for p in files
                                 if p.exists() and p.stat().st_mtime >= today)
    latest = max(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, default=None)
    if latest:
        try:
            with open(latest, "rb") as f:
                head = f.read(2048).decode("utf-8", "replace")
            m = re.search(r'"message_count":\s*(\d+)', head)
            if m:
                data["message_count"] = int(m.group(1))
        except OSError:
            pass
    _STATS.update(ts=now, data=data)
    return data


def probe(scan: ProcScan) -> AgentStatus:
    procs = _match_procs(scan)
    newest = _newest_session_mtime()
    enter = {"type": "iterm", "needle": "hermes"}

    if not procs:
        return AgentStatus(id="hermes", name="Hermes Agent", kind="cli", state="offline",
                           activity="未运行", last_active=newest,
                           enter=enter, has_sessions=True, stats=collect_stats())

    cpu, mem = scan.usage(procs)
    active = newest is not None and (time.time() - newest) < RECENT_WINDOW
    return AgentStatus(id="hermes", name="Hermes Agent", kind="cli",
                       state="busy" if active else "idle",
                       activity="正在执行任务" if active else "空闲等待输入",
                       last_active=newest, pid=procs[0].info["pid"],
                       cpu_percent=cpu or None, mem_mb=mem or None,
                       enter=enter, has_sessions=True, stats=collect_stats())
