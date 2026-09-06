"""deepseek-harness (dsh) 探测器。

`dsh web` 监听 127.0.0.1:3080，HTTP API 需 launch-token 换 cookie（401），
V1 不调其 API：以「进程存在 + 端口可连」判活，以 ~/.dsh/sessions/ 目录
mtime 作为最近活动时间。

会话列表：~/.dsh/sessions/<工作区slug>/session-<uuid>/ 两级目录（内容为
zstd 压缩的对话数据，不读），工作区路径由 slug 贪心还原（isdir 验证）。
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from .base import AgentStatus, ProcScan, newest_mtime

HOST, PORT = "127.0.0.1", 3080
SESSIONS_DIR = Path(os.path.expanduser("~/.dsh/sessions"))
DSH_REPO = os.path.expanduser("~/deepseek-harness")   # 本机 dsh 从源码跑（pnpm）


def _port_alive() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _decode_slug(slug: str) -> str:
    """'--Users-lll-zl-foo-bar--' → 尽量还原工作区路径。

    斜杠被换成了 '-'，只能贪心还原：从左往右优先匹配最长的已存在目录。
    """
    parts = slug.strip("-").split("-")
    cur, i = "", 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            cand = (cur.rstrip("/") + "/" if cur else "/") + "-".join(parts[i:j])
            if os.path.isdir(cand):
                cur, i = cand, j
                break
        else:   # 剩余段拼不成已存在目录：视为最后一段含 '-' 的目录名
            cur = (cur.rstrip("/") + "/" if cur else "/") + "-".join(parts[i:])
            break
    return cur if os.path.isdir(cur) else ""


def list_sessions(limit: int = 12, hidden_ids: frozenset | set = frozenset()) -> dict:
    """dsh 会话 = ~/.dsh/sessions/<工作区>/session-<uuid>；只读目录名与 mtime。"""
    rows = []
    if SESSIONS_DIR.is_dir():
        for ws in SESSIONS_DIR.iterdir():
            if not ws.is_dir():
                continue
            for sess in ws.iterdir():
                if not (sess.is_dir() and sess.name.startswith("session-")):
                    continue
                try:
                    rows.append((sess.name.removeprefix("session-"), ws.name,
                                 sess.stat().st_mtime))
                except OSError:
                    continue
    rows.sort(key=lambda x: x[2], reverse=True)

    home = os.path.expanduser("~")
    visible, hidden = [], []
    for uuid, slug, mtime in rows:
        item = {"id": uuid, "title": f"会话 {uuid[:8]}",
                "cwd": _decode_slug(slug) or home,
                "last_active": mtime, "running": False}
        if uuid in hidden_ids:
            if len(hidden) < 50:
                hidden.append(item)
        else:
            visible.append(item)
        if len(visible) >= limit and len(hidden) >= 50:
            break
    return {"visible": visible[:limit], "hidden": hidden}


_STATS = {"ts": 0.0, "data": {}}


def collect_stats() -> dict:
    """会话总数（60s 缓存）。"""
    now = time.time()
    if now - _STATS["ts"] < 60:
        return _STATS["data"]
    total = 0
    if SESSIONS_DIR.is_dir():
        for ws in SESSIONS_DIR.iterdir():
            if ws.is_dir():
                total += sum(1 for s in ws.iterdir()
                             if s.is_dir() and s.name.startswith("session-"))
    data = {"total_sessions": total}
    _STATS.update(ts=now, data=data)
    return data


def probe(scan: ProcScan) -> list[AgentStatus]:
    procs = scan.with_cmdline("bin.ts", "web") or scan.with_cmdline("dsh", "web")
    port_ok = _port_alive()
    newest = None
    if SESSIONS_DIR.is_dir():
        newest = newest_mtime([p for p in SESSIONS_DIR.iterdir()])

    if not procs and not port_ok:
        return [AgentStatus(
            id="dsh", name="DeepSeek Harness", kind="web", state="offline",
            activity="未运行",
            enter={"type": "url", "url": f"http://{HOST}:{PORT}"},
            has_sessions=True,
            stats=collect_stats(),
        )]

    cpu, mem = scan.usage(procs) if procs else (0.0, 0.0)
    return [AgentStatus(
        id="dsh", name="DeepSeek Harness", kind="web",
        state="running",
        activity=f"Web UI 已启动（{HOST}:{PORT}）",
        last_active=newest,
        pid=procs[0].info["pid"] if procs else None,
        cpu_percent=cpu or None,
        mem_mb=mem or None,
        enter={"type": "url", "url": f"http://{HOST}:{PORT}"},
        has_sessions=True,
        stats=collect_stats(),
        detail={"port": f"{HOST}:{PORT}"},
    )]
