"""The claude subprocess gets only what it needs from the environment, and never outlives a cancelled call."""
import asyncio
import sys

import pytest

from app.llm.claude_cli import ClaudeCLI


def test_the_cli_does_not_inherit_the_apps_secrets(monkeypatch):
    from app.config import get_settings
    get_settings()                     # built before the variables below, so no other test sees them as settings
    for k, v in {"DASHBOARD_PASSWORD": "pw", "TELEGRAM_BOT_TOKEN": "1:x", "DEBUG_TOKEN": "d" * 20, "HA_TOKEN": "h",
                 "ANTHROPIC_API_KEY": "sk", "PATH": "/usr/bin", "HOME": "/home/x", "LC_ALL": "C.UTF-8"}.items():
        monkeypatch.setenv(k, v)
    env = ClaudeCLI(cmd=["true"])._env()
    assert not {"DASHBOARD_PASSWORD", "TELEGRAM_BOT_TOKEN", "DEBUG_TOKEN", "HA_TOKEN", "ANTHROPIC_API_KEY"} & set(env)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x" and env["LC_ALL"] == "C.UTF-8"


async def test_a_cancelled_call_kills_the_cli(tmp_path):
    pidfile = tmp_path / "pid"
    script = tmp_path / "slow.py"
    script.write_text(f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    cli = ClaudeCLI(cmd=[sys.executable, str(script)], timeout_s=60)
    task = asyncio.ensure_future(cli._complete(system="s", prompt="p", schema={}))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if pidfile.exists() and pidfile.read_text():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int(pidfile.read_text())
    import os
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    pytest.fail("the claude subprocess outlived the cancelled call")
