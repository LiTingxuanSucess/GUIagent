#!/usr/bin/env python3
"""Rehearse the Yuanbao login cutover on a throwaway profile.

This never restarts geo-server, never writes the live 176 profile, and never
prints cookie values. It fails at the first broken precheck.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path("/root/workspace/GUIAgent/GEO/server")
LIVE_PROFILE = Path("/root/workspace/GUIAgent/GEO/.chrome-cn-ai-profiles/acct_836d3b506d1b")
LIVE_BIN = ROOT / "bin" / "geo-server"
NEXT_BIN = ROOT / "bin" / "geo-server.next"
YUANBAO_DOMAINS = ("yuanbao.tencent.com", "tencent.com")
PRE_TOKEN = "rehearse-pre-hy-token"
POST_TOKEN = "rehearse-post-hy-token"


def fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), flush=True)
    raise SystemExit(1)


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def file_sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def redact(text: str) -> str:
    for secret in (PRE_TOKEN, POST_TOKEN):
        text = text.replace(secret, "[redacted]")
    return text


def same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def assert_safe_workdir(workdir: Path) -> None:
    if not str(workdir).startswith("/tmp/"):
        fail(f"workdir must stay under /tmp, got {workdir}")
    if same_path(workdir, LIVE_PROFILE) or LIVE_PROFILE in workdir.parents:
        fail("refusing to use the live 176 profile")
    if same_path(workdir, LIVE_BIN.parent) or LIVE_BIN.parent in workdir.parents:
        fail("refusing to use the live binary directory")


def cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def find_pids(needle: str, exclude: str = "") -> list[int]:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        cmd = cmdline(int(name))
        if needle in cmd and (not exclude or exclude not in cmd):
            found.append(int(name))
    return sorted(found)


def exe_inode(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}/exe").st_ino
    except OSError:
        return None


def live_guard() -> dict:
    servers = []
    for pid in find_pids("/GEO/server/bin/geo-server", "geo-server.next"):
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        if exe.endswith("/bin/geo-server"):
            servers.append(pid)
    if len(servers) != 1:
        fail(f"实时状态未知: expected one running geo-server, found {servers}")
    chromes = []
    for pid in find_pids("user-data-dir="):
        cmd = cmdline(pid)
        if "acct_836d3b506d1b" in cmd and "--type=" not in cmd:
            chromes.append(pid)
    if len(chromes) != 1:
        fail(f"实时状态未知: expected one 176 Chrome, found {chromes}")
    inode = exe_inode(servers[0])
    disk_inode = LIVE_BIN.stat().st_ino
    if inode != disk_inode:
        fail(f"running geo-server inode {inode} is not bin/geo-server {disk_inode}")
    if NEXT_BIN.exists() and NEXT_BIN.stat().st_ino == disk_inode:
        fail("bin/geo-server and bin/geo-server.next are the same file")
    store = LIVE_PROFILE / ".session-store.json"
    return {
        "server_pid": servers[0],
        "server_inode": inode,
        "chrome_pid": chromes[0],
        "live_store_mtime_ns": store.stat().st_mtime_ns if store.exists() else None,
        "live_store_inode": store.stat().st_ino if store.exists() else None,
    }


def cookie(name: str, value: str, domain: str, expires: int) -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": expires,
        "http_only": True,
        "secure": True,
        "session": False,
    }


def host_kept(domain: str, keep: set[str]) -> bool:
    host = domain.lstrip(".")
    return any(host == item or host.endswith("." + item) or item in host for item in keep)


def old_running_writeback(fresh: list[dict], previous: list[dict]) -> list[dict]:
    """Match the running binary: need_captcha keeps the old Yuanbao snapshot."""
    keep = set(YUANBAO_DOMAINS)
    kept = [item for item in previous if host_kept(item["domain"], keep)]
    carried = [item for item in fresh if not host_kept(item["domain"], keep)]
    return carried + kept


def write_store(path: Path, cookies: list[dict]) -> None:
    path.write_text(
        json.dumps({"saved_at": "rehearsal", "cookies": cookies, "local_storage": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path.chmod(0o600)


def hy_sha(cookies: list[dict]) -> str:
    found = [item["value"] for item in cookies if item["name"] == "hy_token"]
    if len(found) != 1 or not found[0]:
        fail("fixture lost the single hy_token")
    return sha12(found[0])


def rehearse_cookies(profile: Path) -> dict:
    expires = int(time.time()) + 86400
    pre = [
        cookie("hy_token", PRE_TOKEN, ".tencent.com", expires),
        cookie("_qimei_h38", "rehearse-old-marker", ".tencent.com", expires),
    ]
    post = [
        cookie("hy_token", POST_TOKEN, ".tencent.com", expires),
        cookie("hy_source", "web", ".tencent.com", expires),
        cookie("ds_session_id", "rehearse-other-site", ".deepseek.com", expires),
    ]
    pre_sha = sha12(PRE_TOKEN)
    post_sha = sha12(POST_TOKEN)
    if pre_sha == post_sha:
        fail("synthetic pre and post tokens hash to the same prefix")

    backup = profile / ".session-store.yuanbao-after-login.json"
    write_store(backup, post)
    if hy_sha(json.loads(backup.read_text(encoding="utf-8"))["cookies"]) != post_sha:
        fail("new hy_token backup does not match the post-login fixture")

    written_back = old_running_writeback(post, pre)
    store = profile / ".session-store.json"
    write_store(store, written_back)
    if hy_sha(json.loads(store.read_text(encoding="utf-8"))["cookies"]) != pre_sha:
        fail("old snapshot write-back did not restore the pre-login hy_token")
    names = {item["name"] for item in written_back}
    if "ds_session_id" not in names:
        fail("old write-back dropped a non-yuanbao cookie")
    if "hy_source" in names:
        fail("old write-back kept a fresh yuanbao cookie")

    shutil.copyfile(backup, store)
    store.chmod(0o600)
    if hy_sha(json.loads(store.read_text(encoding="utf-8"))["cookies"]) != post_sha:
        fail("restoring the backup did not return the post-login hy_token")
    prefer = Path(str(store) + ".prefer")
    prefer.write_text("hy_token\n", encoding="utf-8")
    prefer.chmod(0o600)
    if prefer.read_text(encoding="utf-8").strip() != "hy_token":
        fail("prefer file must contain only hy_token")

    profile_cookies = profile / "profile-cookies.json"
    profile_cookies.write_text(json.dumps(pre), encoding="utf-8")
    profile_cookies.chmod(0o600)
    return {"pre_sha256_12": pre_sha, "post_sha256_12": post_sha}


def run_go_prefer(profile: Path) -> dict:
    env = os.environ.copy()
    env["GEO_REHEARSE_DIR"] = str(profile)
    proc = subprocess.run(
        ["/usr/local/go/bin/go", "test", "-count=1", "-run", "TestRehearseYuanbaoPreferFile", "./internal/sessions"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    output = redact(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        fail("go prefer rehearsal failed: " + output[-2000:])
    if PRE_TOKEN in output or POST_TOKEN in output:
        fail("go test output contained a token value")
    result_path = profile / "rehearse-result.json"
    if not result_path.exists():
        fail("go test did not write rehearse-result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("selected_hy_token_sha256_12") != sha12(POST_TOKEN):
        fail("prefer restore selected a different hy_token than the backup")
    if not result.get("prefer_consumed") or not result.get("second_consume_empty"):
        fail("prefer file was not one-shot")
    if (profile / ".session-store.json.prefer").exists():
        fail("prefer file still exists after consumption")
    return result


def rehearse_binary_rollback(workdir: Path, before: dict) -> dict:
    if not LIVE_BIN.is_file() or not NEXT_BIN.is_file():
        fail("missing bin/geo-server or bin/geo-server.next")
    stage = workdir / "bin"
    stage.mkdir()
    installed = stage / "geo-server"
    candidate = stage / "geo-server.next"
    shutil.copy2(LIVE_BIN, installed)
    shutil.copy2(NEXT_BIN, candidate)
    original = file_sha12(installed)
    nxt = file_sha12(candidate)
    if original == nxt:
        fail("rehearsal copies of the old and new binaries are identical")
    backup = stage / "geo-server.before-yuanbao-login"
    shutil.copy2(installed, backup)
    installed.unlink()
    shutil.copy2(candidate, installed)
    if file_sha12(installed) != nxt:
        fail("swap did not install the new binary into the rehearsal directory")
    installed.unlink()
    shutil.copy2(backup, installed)
    if file_sha12(installed) != original:
        fail("rollback did not restore the original binary bytes")
    after = live_guard()
    if after["server_inode"] != before["server_inode"] or after["server_pid"] != before["server_pid"]:
        fail("live geo-server changed during the binary rehearsal")
    if after["chrome_pid"] != before["chrome_pid"]:
        fail("176 Chrome changed during the binary rehearsal")
    return {
        "original_sha256_12": original,
        "next_sha256_12": nxt,
        "rollback_sha256_12": file_sha12(installed),
        "backup_exists": backup.is_file(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.live:
        fail("refusing to touch the live service or the 176 captcha session")
    before = live_guard()
    workdir = Path(tempfile.mkdtemp(prefix="geo-yuanbao-cutover-rehearsal-"))
    assert_safe_workdir(workdir)
    profile = workdir / "profile"
    profile.mkdir()
    try:
        cookies = rehearse_cookies(profile)
        prefer = run_go_prefer(profile)
        binary = rehearse_binary_rollback(workdir, before)
        after = live_guard()
        if after != before and (
            after["server_pid"] != before["server_pid"]
            or after["chrome_pid"] != before["chrome_pid"]
            or after["server_inode"] != before["server_inode"]
            or after["live_store_inode"] != before["live_store_inode"]
        ):
            fail("live service identity changed during rehearsal")
        report = {
            "ok": True,
            "workdir": str(workdir),
            "backup_post_hy_token_sha256_12": cookies["post_sha256_12"],
            "old_writeback_hy_token_sha256_12": cookies["pre_sha256_12"],
            "recovered_hy_token_sha256_12": prefer["selected_hy_token_sha256_12"],
            "prefer_consumed": prefer["prefer_consumed"],
            "second_consume_empty": prefer["second_consume_empty"],
            "without_prefer_includes_hy_token": prefer["without_prefer_includes_hy_token"],
            "binary": binary,
            "live_before": before,
            "live_after": after,
            "live_store_mtime_unchanged": before["live_store_mtime_ns"] == after["live_store_mtime_ns"],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    except SystemExit:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        fail(redact(str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
