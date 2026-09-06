"""「进入 agent」动作：激活 iTerm2 匹配窗口 / open -a / 打开 URL / 会话级跳转。

全部为 best-effort：AppleScript 首次运行会触发 macOS 自动化授权弹窗，
拒绝后动作退化为「仅把 iTerm2 带到前台」。
"""
from __future__ import annotations

import shlex
import subprocess

from .agents.base import AgentStatus


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _iterm_script(needle: str) -> str:
    return f'''
    tell application "iTerm2"
        activate
        set _found to false
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if (not _found) and (name of s contains "{_esc(needle)}") then
                        tell t to select
                        set index of w to 1
                        set _found to true
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    '''


def enter(status: AgentStatus) -> str:
    spec = status.enter or {}
    kind = spec.get("type")
    if kind == "app":
        subprocess.Popen(["open", "-a", spec["app"]])
        return f"已打开应用 {spec['app']}"
    if kind == "url":
        subprocess.Popen(["open", spec["url"]])
        return f"已在浏览器打开 {spec['url']}"
    if kind == "iterm":
        needle = (spec.get("needle") or "").strip()
        try:
            r = subprocess.run(
                ["osascript", "-e", _iterm_script(needle)],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            return "激活 iTerm2 超时（可能正等待自动化授权）"
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "osascript 执行失败")
        hit = "并已定位到对应窗口" if needle else ""
        return f"已激活 iTerm2{hit}"
    raise ValueError(f"未知动作类型: {kind!r}")


def open_browser(url: str) -> str:
    """用默认浏览器打开 URL（Web 类 agent 的会话入口）。"""
    subprocess.Popen(["open", url])
    return f"已在浏览器打开 {url}"


def open_in_iterm(cwd: str, command: str) -> str:
    """在 iTerm2 新标签里 cd 到 cwd 并执行 command（如 claude --resume <id>）。"""
    shell_cmd = f"cd {shlex.quote(cwd)} && {command}"
    script = f'''
    tell application "iTerm2"
        activate
        if (count of windows) = 0 then
            create window with default profile
        else
            tell current window to create tab with default profile
        end if
        delay 0.3
        tell current session of current window to write text "{_esc(shell_cmd)}"
    end tell
    '''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return "打开 iTerm2 超时（可能正等待自动化授权，请在弹窗中允许）"
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript 执行失败")
    return f"已在 iTerm2 新标签执行：{command}"
