"""探测器基础设施：统一状态模型 + 进程扫描快照。"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# 状态语义：waiting 等待审批 / busy 忙碌 / running 运行中 / idle 空闲 / offline 未运行
STATES = ("waiting", "busy", "running", "idle", "offline")


def today_start() -> float:
    """本地今天 0 点（epoch 秒），供「今日会话数」统计。"""
    return datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def ms_to_ts(ms: int | float | None) -> float | None:
    """各 agent 落盘的时间戳多为 epoch 毫秒，转成秒。"""
    if not ms:
        return None
    return float(ms) / 1000 if ms > 1e12 else float(ms)


@dataclass
class AgentStatus:
    id: str
    name: str
    kind: str                      # cli | app | web
    state: str = "offline"
    activity: str = ""             # 一句话「在干嘛」
    project: str | None = None     # 工作目录名
    title: str | None = None       # 当前会话标题
    model: str | None = None
    last_active: float | None = None   # epoch 秒
    pid: int | None = None
    cpu_percent: float | None = None
    mem_mb: float | None = None
    enter: dict = field(default_factory=dict)   # control.py 的动作描述
    detail: dict = field(default_factory=dict)
    has_sessions: bool = False                  # 卡片显示「会话」入口
    stats: dict = field(default_factory=dict)   # 身份/统计行数据（model/version/今日会话数/tok/增删）

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "state": self.state, "activity": self.activity,
            "project": self.project, "title": self.title, "model": self.model,
            "last_active": self.last_active,
            "cpu": self.cpu_percent, "mem_mb": self.mem_mb,
            "detail": self.detail, "enter": self.enter,
            "has_sessions": self.has_sessions, "stats": self.stats,
        }


class ProcScan:
    """一次快照扫描全部进程，供各探测器复用查询。

    cpu_percent 采用 psutil 的差值语义：首次调用返回 0，
    下个快照周期起即为两次调用之间的真实占用。
    """

    def __init__(self) -> None:
        self.procs: list[psutil.Process] = []
        for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                _ = p.cpu_percent(None)  # 建立差值基准
                self.procs.append(p)
            except (psutil.Error, OSError):
                continue

    @staticmethod
    def _cmdline(p: psutil.Process) -> str:
        try:
            return " ".join(p.info.get("cmdline") or [])
        except (psutil.Error, OSError):
            return ""

    def with_cmdline(self, *needles: str, exclude: tuple[str, ...] = ()) -> list[psutil.Process]:
        out = []
        for p in self.procs:
            cl = self._cmdline(p)
            if not cl or not all(n in cl for n in needles):
                continue
            if any(e in cl for e in exclude):
                continue
            out.append(p)
        return out

    def with_exe_path(self, fragment: str) -> list[psutil.Process]:
        out = []
        for p in self.procs:
            exe = p.info.get("exe") or ""
            if fragment in exe or fragment in self._cmdline(p):
                out.append(p)
        return out

    @staticmethod
    def usage(procs: list[psutil.Process]) -> tuple[float, float]:
        """返回 (cpu%, 内存MB) 合计。"""
        cpu = mem = 0.0
        for p in procs:
            try:
                cpu += p.cpu_percent(None)
                mem += (p.memory_info().rss) / 1024 / 1024
            except (psutil.Error, OSError):
                continue
        return round(cpu, 1), round(mem, 1)


def newest_mtime(paths: list[Path]) -> float | None:
    """目录/文件集合中最新的 mtime（epoch 秒）。"""
    newest = None
    for p in paths:
        try:
            m = p.stat().st_mtime
            if newest is None or m > newest:
                newest = m
        except OSError:
            continue
    return newest
