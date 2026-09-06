"""任务流水线 runner。

把总任务拆成若干步骤，逐步以无头模式调用各 agent 的 CLI，
上一步输出可经 {input} 占位符注入下一步提示词。输出实时入内存环形缓冲
并落盘 runs/<task_id>/step_<n>.log。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .registry import TASK_AGENTS

RUNS_DIR = str(Path(__file__).resolve().parents[2] / "runs")
STEP_TIMEOUT = 600          # 单步超时（秒）
OUTPUT_CAP = 40_000         # 内存里保留的输出尾部字符数
INPUT_INJECT_CAP = 6_000    # 注入 {input} 的上一节输出最大长度


def _cmd(agent: str, prompt: str) -> list[str]:
    if agent == "claude":
        return ["claude", "-p", prompt]
    if agent == "codex":
        return ["codex", "exec", "--skip-git-repo-check", prompt]
    if agent == "mimo":
        return ["mimo", "run", prompt]
    if agent == "gemini":
        return ["gemini", "-p", prompt]
    if agent == "dsh":
        return ["npx", "-y", "@deepseek-ai/dsh", "--profile", "headless", prompt]
    raise ValueError(f"不支持派任务的 agent: {agent}")


@dataclass
class Step:
    agent: str
    prompt: str
    status: str = "pending"    # pending | running | done | error | cancelled
    output: str = ""
    started_at: float | None = None
    ended_at: float | None = None
    returncode: int | None = None
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = {k: v for k, v in {
            "agent": self.agent, "prompt": self.prompt, "status": self.status,
            "output": self.output[-4000:],
            "started_at": self.started_at, "ended_at": self.ended_at,
            "returncode": self.returncode,
        }.items()}
        return d


@dataclass
class Task:
    id: str
    goal: str
    steps: list[Step]
    status: str = "running"    # running | done | error | cancelled
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "goal": self.goal, "status": self.status,
            "created_at": self.created_at,
            "steps": [s.to_dict() for s in self.steps],
        }


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def start(self, goal: str, step_specs: list[dict]) -> Task:
        tid = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        steps = [Step(agent=s["agent"], prompt=s["prompt"]) for s in step_specs]
        task = Task(id=tid, goal=goal, steps=steps)
        self.tasks[tid] = task
        asyncio.get_running_loop().create_task(self._run(task))
        return task

    def cancel(self, tid: str) -> bool:
        task = self.tasks.get(tid)
        if not task or task.status != "running":
            return False
        task.status = "cancelled"
        for s in task.steps:
            if s.status == "running" and s.proc and s.proc.returncode is None:
                self._kill_group(s.proc)
                s.status = "cancelled"
                s.ended_at = time.time()
            elif s.status in ("pending",):
                s.status = "cancelled"
        return True

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def _stream(self, step: Step, proc: asyncio.subprocess.Process, log_path: str) -> None:
        loop = asyncio.get_running_loop()
        logf = open(log_path, "ab")

        async def pump(stream):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", "replace")
                step.output = (step.output + text)[-OUTPUT_CAP:]
                await loop.run_in_executor(None, logf.write, chunk)

        try:
            await asyncio.gather(pump(proc.stdout), pump(proc.stderr))
        finally:
            logf.close()

    async def _run(self, task: Task) -> None:
        os.makedirs(os.path.join(RUNS_DIR, task.id), exist_ok=True)
        prev_output = ""
        try:
            for i, step in enumerate(task.steps):
                if task.status != "running":
                    break
                prompt = step.prompt.replace("{input}", prev_output[-INPUT_INJECT_CAP:])
                workdir = os.path.join(RUNS_DIR, task.id, f"step_{i + 1}")
                os.makedirs(workdir, exist_ok=True)
                log_path = os.path.join(RUNS_DIR, task.id, f"step_{i + 1}.log")

                step.status = "running"
                step.started_at = time.time()
                exe = _cmd(step.agent, prompt)[0]
                if shutil.which(exe) is None:
                    step.status = "error"
                    step.output = f"找不到可执行文件 {exe}（PATH 不含它）"
                    step.ended_at = time.time()
                    task.status = "error"
                    return
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *_cmd(step.agent, prompt),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.DEVNULL,
                        cwd=workdir,
                        start_new_session=True,   # 独立进程组，取消时可整组终止
                    )
                except OSError as exc:
                    step.status = "error"
                    step.output = f"启动失败: {exc}"
                    step.ended_at = time.time()
                    task.status = "error"
                    return
                step.proc = proc

                try:
                    await asyncio.wait_for(
                        asyncio.gather(self._stream(step, proc, log_path), proc.wait()),
                        timeout=STEP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self._kill_group(proc)
                    step.output += "\n[看板] 步骤超时，已终止"
                    step.status = "error"
                except asyncio.CancelledError:
                    self._kill_group(proc)
                    raise

                step.returncode = proc.returncode
                if step.status == "running":
                    step.status = "done" if proc.returncode == 0 else "error"
                step.ended_at = time.time()

                if step.status != "done":
                    task.status = "error" if step.status == "error" else "cancelled"
                    return
                prev_output = step.output

            if task.status == "running":
                task.status = "done"
        except asyncio.CancelledError:
            task.status = "cancelled"
            raise
        except Exception as exc:  # 任何意外都不让 runner 崩掉
            task.status = "error"
            if task.steps:
                last = task.steps[-1]
                last.output = (last.output or "") + f"\n[看板] 内部错误: {exc}"

    def all(self) -> list[dict]:
        return [t.to_dict() for t in
                sorted(self.tasks.values(), key=lambda t: t.created_at, reverse=True)]


def validate_steps(step_specs: list[dict]) -> str | None:
    """返回错误信息或 None。"""
    if not step_specs:
        return "至少需要一个步骤"
    for s in step_specs:
        if not isinstance(s, dict) or s.get("agent") not in TASK_AGENTS:
            return f"agent 必须是 {TASK_AGENTS} 之一"
        if not (s.get("prompt") or "").strip():
            return "每个步骤的提示词不能为空"
    return None
