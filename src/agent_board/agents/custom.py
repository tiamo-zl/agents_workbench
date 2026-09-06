"""自定义 agent 探测器：由 config.json 的 custom 列表数据驱动，无需写代码。

三种类型：
- app  桌面应用：按进程 exe/命令行里的特征串（一般是 "<名>.app"）判运行
- cli  命令行 agent：按命令行包含的识别串判运行；进入 = 激活 iTerm2
- web  本机 Web 服务：TCP 端口探活判运行；进入 = 打开 URL
"""
from __future__ import annotations

import socket
from pathlib import Path

from .base import AgentStatus, ProcScan


def _app_name(match: str) -> str:
    return match[:-4] if match.endswith(".app") else match


def _make_app(cfg: dict):
    def probe(scan: ProcScan) -> AgentStatus:
        procs = scan.with_exe_path(cfg["match"])
        if not procs:
            return AgentStatus(id=cfg["id"], name=cfg["name"], kind="app",
                               state="offline", activity="未运行",
                               enter={"type": "app", "app": _app_name(cfg["match"])})
        cpu, mem = scan.usage(procs)
        return AgentStatus(id=cfg["id"], name=cfg["name"], kind="app",
                           state="running", activity="运行中",
                           pid=procs[0].info["pid"], cpu_percent=cpu or None,
                           mem_mb=mem or None,
                           enter={"type": "app", "app": _app_name(cfg["match"])})
    probe._agent_id = cfg["id"]   # 降级时能定位到具体 agent
    return probe


def _make_cli(cfg: dict):
    def probe(scan: ProcScan) -> AgentStatus:
        procs = scan.with_cmdline(cfg["match"])
        if not procs:
            return AgentStatus(id=cfg["id"], name=cfg["name"], kind="cli",
                               state="offline", activity="未运行",
                               enter={"type": "iterm", "needle": cfg["match"]})
        cpu, mem = scan.usage(procs)
        return AgentStatus(id=cfg["id"], name=cfg["name"], kind="cli",
                           state="running", activity="进程运行中",
                           pid=procs[0].info["pid"], cpu_percent=cpu or None,
                           mem_mb=mem or None,
                           enter={"type": "iterm", "needle": cfg["match"]})
    probe._agent_id = cfg["id"]   # 降级时能定位到具体 agent
    return probe


def _make_web(cfg: dict):
    host, port = cfg.get("host") or "127.0.0.1", int(cfg["port"])
    url = cfg.get("url") or f"http://{host}:{port}"

    def probe(scan: ProcScan) -> AgentStatus:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                alive = True
        except OSError:
            alive = False
        return AgentStatus(id=cfg["id"], name=cfg["name"], kind="web",
                           state="running" if alive else "offline",
                           activity=f"服务在线（{host}:{port}）" if alive else "服务未响应",
                           enter={"type": "url", "url": url})
    probe._agent_id = cfg["id"]   # 降级时能定位到具体 agent
    return probe


def make_probes(cfgs: list[dict]):
    """按配置动态产出探测器；单条配置非法只跳过该条。"""
    makers = {"app": _make_app, "cli": _make_cli, "web": _make_web}
    out = []
    for cfg in cfgs:
        maker = makers.get(cfg.get("type"))
        if not maker:
            continue
        try:
            out.append(maker(cfg))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def suggestions() -> list[dict]:
    """/Applications 下所有应用名，供添加表单做候选（不含子目录）。"""
    apps = []
    for p in Path("/Applications").glob("*.app"):
        apps.append(p.name)
    return sorted(apps)
