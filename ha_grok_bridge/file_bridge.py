#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
DATA = Path("/data")
CONFIG = Path("/config")
WORK = DATA / "bridge-work"
SNAPSHOTS = DATA / "snapshots"
OPTIONS = DATA / "options.json"

# Files/directories that are runtime state, secrets, credentials, or large databases.
# They must never be copied into the Git repository by the automatic HA -> Git sync.
EXCLUDED_NAMES = {
    ".git",
    ".storage",
    ".cloud",
    ".HA_VERSION",
    "home-assistant.log",
    "home-assistant.log.1",
    "home-assistant.log.fault",
    "home-assistant_v2.db",
    "home-assistant_v2.db-shm",
    "home-assistant_v2.db-wal",
    "home-assistant_v2.db-journal",
    "secrets.yaml",
}
EXCLUDED_DIRS = {"tts", "media", "backups"}


def log(message: str) -> None:
    print(f"[file-bridge] {message}", flush=True)


def run_git(
    args: list[str],
    cwd: Path = WORK,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    log(f"git command: git {' '.join(args)}")
    process = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if process.stdout.strip():
        log(f"git stdout: {process.stdout.strip()}")
    if process.stderr.strip():
        log(f"git stderr: {process.stderr.strip()}")
    if check and process.returncode != 0:
        raise RuntimeError(
            f"git exited with {process.returncode}: "
            f"{process.stderr.strip() or process.stdout.strip()}"
        )
    return process


def load_options() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "poll_interval": 60,
        "config_repo": "git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git",
        "branch": "main",
        "sync_config_to_git": True,
    }
    if OPTIONS.is_file():
        try:
            value = json.loads(OPTIONS.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                defaults.update(value)
        except Exception as exc:
            log(f"options warning: {exc}")
    return defaults


def ensure_repo(repo_url: str, branch: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not (WORK / ".git").is_dir():
        if WORK.exists():
            shutil.rmtree(WORK)
        run_git(["clone", "--no-checkout", repo_url, str(WORK)], cwd=DATA)
        run_git(["checkout", "-B", branch, f"origin/{branch}"], cwd=WORK)
    else:
        run_git(["remote", "set-url", "origin", repo_url])
        run_git(["fetch", "--prune", "origin"], cwd=WORK)
        run_git(["checkout", branch], cwd=WORK)
        run_git(["reset", "--hard", f"origin/{branch}"], cwd=WORK)


def validate() -> tuple[bool, str]:
    if not (WORK / ".git").is_dir():
        return False, "repository not initialized"
    process = run_git(["fsck", "--no-progress"], check=False)
    if process.returncode == 0:
        return True, "repository valid"
    return False, "repository invalid"


def snapshot() -> Path | None:
    if not CONFIG.is_dir():
        return None
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    target = SNAPSHOTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copytree(CONFIG, target, ignore=ignore_config)
    return target


def ignore_config(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in EXCLUDED_NAMES or name in EXCLUDED_DIRS:
            ignored.add(name)
    return ignored


def sync_config_to_worktree() -> int:
    if not CONFIG.is_dir():
        raise RuntimeError("/config is not mapped")
    WORK.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in CONFIG.iterdir():
        if source.name in EXCLUDED_NAMES or source.name in EXCLUDED_DIRS:
            continue
        destination = WORK / source.name
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination, ignore=ignore_config)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        copied += 1
    return copied


def sync_to_github(branch: str) -> bool:
    copied = sync_config_to_worktree()
    log(f"/config sync: {copied} top-level items prepared")
    run_git(["add", "-A"], cwd=WORK)
    changes = run_git(["status", "--short"], cwd=WORK, check=False).stdout.strip()
    if not changes:
        log("/config unchanged; nothing to commit")
        return False

    run_git(["config", "user.name", "HA File Sync Bridge"], cwd=WORK)
    run_git(
        ["config", "user.email", "ha-file-sync-bridge@localhost"],
        cwd=WORK,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    run_git(["commit", "-m", f"Sync Home Assistant /config - {timestamp}"], cwd=WORK)
    run_git(["push", "origin", branch], cwd=WORK)
    log("HA /config synchronized to GitHub")
    return True


def status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": VERSION,
        "time": datetime.now(timezone.utc).isoformat(),
        "repository": str(WORK),
        "config": str(CONFIG),
    }
    if (WORK / ".git").is_dir():
        result["git_status"] = run_git(
            ["status", "--short"], cwd=WORK, check=False
        ).stdout.splitlines()
    else:
        result["git_status"] = None
    result["snapshots"] = (
        len(list(SNAPSHOTS.iterdir())) if SNAPSHOTS.is_dir() else 0
    )
    return result


def main() -> None:
    log(f"HA File Sync Bridge {VERSION}")
    while True:
        try:
            cfg = load_options()
            repo_url = str(cfg["config_repo"])
            branch = str(cfg.get("branch", "main"))
            ensure_repo(repo_url, branch)
            ok, message = validate()
            log(message)
            if not ok:
                raise RuntimeError(message)
            log("repository access OK")

            if bool(cfg.get("sync_config_to_git", True)):
                snapshot()
                sync_to_github(branch)
        except Exception as exc:
            log(f"REQUEST FAILED: {exc}")
        time.sleep(max(5, int(load_options().get("poll_interval", 60))))


if __name__ == "__main__":
    main()
