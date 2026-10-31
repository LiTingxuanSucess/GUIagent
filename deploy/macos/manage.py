"""Manage the local Agent Console with the current user's launchd service."""

import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "cn.revai.guiagent.console"
DOMAIN = f"gui/{os.getuid()}"
SERVICE = f"{DOMAIN}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
URL = "http://127.0.0.1:8000"


def run(*args, check=True):
    return subprocess.run(
        ["/bin/launchctl", *args], check=check, capture_output=True, text=True
    )


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "stop":
        result = run("bootout", SERVICE, check=False)
        if result.returncode and run("print", SERVICE, check=False).returncode == 0:
            raise RuntimeError(result.stderr.strip())
        print("Agent Console 已停止")
        return
    if action == "start":
        logs = ROOT / "logs/macos-console"
        logs.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        settings = {
            "Label": LABEL,
            "ProgramArguments": [
                str(ROOT / ".venv/bin/python"), "-m", "uvicorn",
                "deploy.macos.console:app", "--host", "127.0.0.1", "--port", "8000",
            ],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
            "StandardOutPath": str(logs / "stdout.log"),
            "StandardErrorPath": str(logs / "stderr.log"),
        }
        PLIST.write_bytes(plistlib.dumps(settings))
        if run("print", SERVICE, check=False).returncode != 0:
            result = run("bootstrap", DOMAIN, str(PLIST), check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
        run("kickstart", "-k", SERVICE)
        for _ in range(30):
            try:
                with urllib.request.urlopen(URL + "/api/health", timeout=1) as response:
                    if response.status == 200:
                        print(f"Agent Console 已启动：{URL}")
                        return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError(f"启动未就绪，请查看 {logs / 'stderr.log'}")
    if action == "status":
        result = run("print", SERVICE, check=False)
        if result.returncode:
            print("Agent Console 未运行")
            raise SystemExit(1)
        for line in result.stdout.splitlines():
            if line.strip().startswith(("state =", "pid =", "last exit code =")):
                print(line.strip())
        with urllib.request.urlopen(URL + "/api/health", timeout=3) as response:
            print(f"HTTP {response.status} — {URL}")
        return
    raise SystemExit("用法：manage.py start|stop|status")


if __name__ == "__main__":
    main()
