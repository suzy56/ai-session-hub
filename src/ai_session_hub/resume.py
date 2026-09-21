from __future__ import annotations

import os
import shutil
import signal
import subprocess
import termios
from pathlib import Path

from ai_session_hub.adapters import get_adapter
from ai_session_hub.models import LaunchSpec, SessionRef, Unavailable

UNAVAILABLE_DESCRIPTIONS: dict[str, str] = {
    "missing_executable": "未找到 CLI 可执行文件，请确认工具已正确安装并在 PATH 中。",
    "missing_source": "会话源文件或数据库不存在或已被移动。",
    "missing_cwd": "会话记录的工作目录不存在，无法安全恢复。",
    "unknown_cwd": "未记录会话的工作目录，拒绝推测或使用当前目录。",
    "unsupported_profile": "配置的 Profile 不受支持或与本地存储结构不符。",
    "unsupported_resume": "当前会话不支持通过原生 CLI 恢复。",
    "unverified_compatibility": "环境或配置文件尚未初始化，无法恢复。",
    "ambiguous_identity": "检测到多个来源匹配该 ID，身份存在歧义，拒绝恢复。",
    "archived": "会话处于归档状态，请先在原工具中取消归档。",
    "secondary_session": "子代理或顾问记录不支持作为顶级会话恢复。",
    "invalid_identity": "会话标识符格式无效。",
    "metadata_only": "该记录仅包含元数据用量归档，无原生会话转录。",
}


def get_unavailable_message(unavailable: Unavailable) -> str:
    """Return a descriptive Chinese reason for an unavailable resume action."""
    desc = UNAVAILABLE_DESCRIPTIONS.get(unavailable.code, "无法恢复会话。")
    if unavailable.reason:
        return f"{desc}\n详细信息: {unavailable.reason}"
    return desc


def prepare_resume(ref: SessionRef) -> LaunchSpec | Unavailable:
    """Validate and prepare a LaunchSpec for resuming a session."""
    adapter = get_adapter(ref.source.tool)
    if not adapter:
        return Unavailable("unsupported_resume", f"No adapter registered for tool '{ref.source.tool}'")

    result = adapter.prepare_resume(ref)
    if isinstance(result, Unavailable):
        return result

    # Resolve executable to absolute path
    exe_name = result.argv[0]
    resolved_exe = shutil.which(exe_name)
    if not resolved_exe:
        return Unavailable("missing_executable", f"Executable '{exe_name}' not found on PATH")

    # Re-verify cwd
    if not result.cwd.is_dir():
        return Unavailable("missing_cwd", f"Working directory does not exist: {result.cwd}")

    # Return LaunchSpec with absolute executable
    new_argv = (str(Path(resolved_exe).resolve()), *result.argv[1:])
    return LaunchSpec(
        argv=new_argv,
        cwd=result.cwd.resolve(),
        env_overrides=dict(result.env_overrides),
        env_remove=tuple(result.env_remove),
    )


def run_foreground(spec: LaunchSpec) -> int:
    """Execute the native CLI in the foreground with terminal handoff."""
    env = os.environ.copy()
    for k in spec.env_remove:
        env.pop(k, None)
    env.update(spec.env_overrides)
    terminal_state = termios.tcgetattr(0) if os.isatty(0) else None

    # Use a caught no-op SIGINT handler in parent during wait (not SIG_IGN, which children inherit)
    def _sigint_handler(signum: int, frame: object) -> None:
        pass

    old_handler = signal.signal(signal.SIGINT, _sigint_handler)
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            list(spec.argv),
            cwd=str(spec.cwd),
            env=env,
            shell=False,
        )
        ret = proc.wait()
        return ret
    except Exception:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
        raise
    finally:
        signal.signal(signal.SIGINT, old_handler)
        if terminal_state is not None:
            termios.tcsetattr(0, termios.TCSANOW, terminal_state)
