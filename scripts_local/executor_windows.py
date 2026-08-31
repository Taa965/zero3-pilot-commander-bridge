#!/usr/bin/env python3
"""Windows 验证通道执行器。

一轮动作：
1. git pull 信箱仓库（fetch --prune + rebase，冲突则本轮放弃）
2. 认领 pending/*.json（原子移入 running/）
3. 按白名单执行（shell / file_check / git_status / flutter_analyze）
4. 结果写 results/<id>.json + done/<id>.json，running/ 清除
5. 审计日志先写后执行
6. 有变更则 commit + push（禁 force；冲突 fetch/rebase 重试）

退出码：0=正常（含无任务），1=配置错误，2=push 持续失败（结果已留本地）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(r"C:/Users/aaaa/Documents/zero3-pilot-commander-bridge-mailbox")
WV = REPO / "windows-verify"
AUDIT = WV / "events" / "audit.jsonl"
ALLOWED_PREFIXES = [r"C:/Users/aaaa/Documents/", r"C:/Users/aaaa/Desktop/0/"]
MAX_TIMEOUT = 1800
TAIL = 4000
TZ = timezone(timedelta(hours=8))


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(action: str, **kw) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": now(), "action": action, **kw}
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sh(args: list[str], cwd: Path | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return 124, "[timeout]"
    except Exception as exc:  # noqa: BLE001
        return 1, f"[executor error] {exc}"


def git(*args: str, timeout: int = 60) -> tuple[int, str]:
    return sh(["git", *args], cwd=REPO, timeout=timeout)


def pull() -> bool:
    rc, out = git("fetch", "--prune", "origin")
    if rc != 0:
        log("pull_fail", stage="fetch", err=out[-500:])
        return False
    rc, out = git("rebase", "origin/main")
    if rc != 0:
        git("rebase", "--abort")
        log("pull_fail", stage="rebase", err=out[-500:])
        return False
    return True


def push() -> bool:
    for attempt in range(3):
        rc, out = git("push", "origin", "HEAD:main")
        if rc == 0:
            return True
        git("fetch", "--prune", "origin")
        rc2, _ = git("rebase", "origin/main")
        if rc2 != 0:
            git("rebase", "--abort")
            log("push_fail", stage="rebase", err=out[-500:])
            return False
        log("push_retry", attempt=attempt + 1)
    log("push_fail", stage="final")
    return False


def workdir_allowed(wd: str) -> bool:
    norm = wd.replace("\\", "/").rstrip("/") + "/"
    return any(norm.startswith(p) for p in ALLOWED_PREFIXES)


def tail(s: str) -> str:
    return s[-TAIL:] if len(s) > TAIL else s


def execute(env: dict) -> dict:
    t0 = time.monotonic()
    status, exit_code, out, err = "ok", 0, "", ""
    typ = env.get("type")
    wd = env.get("workdir", "")
    timeout_s = min(int(env.get("timeout_s", 300)), MAX_TIMEOUT)

    if not workdir_allowed(wd):
        return {"status": "error", "exit_code": None, "stdout_tail": "",
                "stderr_tail": "", "reason": "workdir_not_allowed",
                "started_at": now(), "finished_at": now(),
                "duration_s": round(time.monotonic() - t0, 1),
                "artifacts": [], "notes": f"workdir={wd}"}

    cwd = Path(wd) if wd else None

    if typ == "shell":
        cmd = env.get("command", "")
        if not cmd or "\x00" in cmd:
            return error_result(env, "invalid_command", t0)
        rc, out = sh(["bash", "-c", cmd], cwd=cwd, timeout=timeout_s)
        exit_code = rc
    elif typ == "file_check":
        lines = []
        for p in env.get("paths", []):
            fp = Path(p)
            lines.append(f"{p}\t{'EXISTS size=' + str(fp.stat().st_size) if fp.exists() else 'MISSING'}")
        out = "\n".join(lines)
    elif typ == "git_status":
        rc, out = sh(["git", "status", "--porcelain"], cwd=cwd, timeout=30)
        rc2, log5 = sh(["git", "log", "--oneline", "-5"], cwd=cwd, timeout=30)
        out = out + "\n-- recent --\n" + log5
        exit_code = rc
    elif typ == "flutter_analyze":
        rc, out = sh(["flutter", "analyze"], cwd=cwd, timeout=timeout_s)
        exit_code = rc
    else:
        return error_result(env, "unknown_type", t0)

    if exit_code == 124:
        status = "timeout"
    elif exit_code != 0:
        status = "fail"

    parts = out.split("\n[stderr]\n", 1)
    stdout_t, stderr_t = tail(parts[0]), tail(parts[1]) if len(parts) > 1 else ""
    return {"status": status, "exit_code": exit_code,
            "stdout_tail": stdout_t, "stderr_tail": stderr_t,
            "started_at": env.get("_claimed_at", now()),
            "finished_at": now(),
            "duration_s": round(time.monotonic() - t0, 1),
            "artifacts": [], "executor": "hermes-windows-1", "notes": ""}


def error_result(env: dict, reason: str, t0: float) -> dict:
    return {"status": "error", "exit_code": None, "stdout_tail": "",
            "stderr_tail": "", "reason": reason, "started_at": now(),
            "finished_at": now(), "duration_s": round(time.monotonic() - t0, 1),
            "artifacts": [], "executor": "hermes-windows-1", "notes": ""}


def process_one(path: Path) -> None:
    try:
        env = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log("parse_fail", file=path.name, err=str(exc)[:300])
        path.unlink(missing_ok=True)
        return
    wid = env.get("id", path.stem)
    claimed_at = now()
    env["_claimed_at"] = claimed_at

    running = WV / "running" / f"{wid}.json"
    log("claim", id=wid, type=env.get("type"))
    running.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, running)  # 原子认领

    result = execute(env)
    log("executed", id=wid, status=result["status"], exit_code=result["exit_code"],
        duration_s=result["duration_s"])

    (WV / "results").mkdir(parents=True, exist_ok=True)
    (WV / "results" / f"{wid}.json").write_text(
        json.dumps({**{k: v for k, v in env.items() if not k.startswith("_")},
                    **result}, ensure_ascii=False, indent=2), encoding="utf-8")
    (WV / "done").mkdir(parents=True, exist_ok=True)
    (WV / "done" / f"{wid}.json").write_text(
        json.dumps({"id": wid, "status": result["status"],
                    "finished_at": result["finished_at"]}, ensure_ascii=False),
        encoding="utf-8")
    running.unlink(missing_ok=True)


def main() -> int:
    if not REPO.exists():
        print(f"mailbox repo missing: {REPO}", file=sys.stderr)
        return 1
    if not pull():
        return 0  # 本轮放弃，下轮再试
    pend = sorted((WV / "pending").glob("*.json")) if (WV / "pending").is_dir() else []
    if not pend:
        return 0
    for p in pend:
        process_one(p)
    git("add", "-A", "windows-verify")
    rc, out = git("commit", "-m", f"windows-verify: execute {len(pend)} command(s) [hermes-windows-1]",
                  timeout=30)
    if rc == 0:
        push()
    else:
        log("commit_noop", err=out[-200:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
