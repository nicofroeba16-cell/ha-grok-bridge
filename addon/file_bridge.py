#!/usr/bin/env python3
"""HA File Sync Bridge 0.5.0: structured Git file synchronization only."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

DATA = Path("/data")
CONFIG = Path("/config")
WORK = DATA / "bridge-work"
BACKUPS = DATA / "backups"
LAST_REQUEST = DATA / "last_request_id"
LAST_GOOD = DATA / "last_known_good.json"
OPTIONS = DATA / "options.json"
REQUEST_PATH = "bridge/request.json"
STATUS_PATH = "bridge/status.json"
META_PATH = "bridge/snapshot.json"
SUPERVISOR_CHECK_URL = "http://supervisor/core/check"
ALLOWED_EXACT = frozenset({"configuration.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml", "go2rtc.yaml"})
ALLOWED_PREFIXES = ("packages/", "dashboards/", "themes/", "www/")
ALLOWED_COMPONENTS: frozenset[str] = frozenset()
ALLOWED_SCOPES = {"core", "automations", "scripts", "scenes", "packages", "dashboards", "themes", "www", "go2rtc", "custom_components"}
DENIED_EXACT = frozenset({"secrets.yaml", "home-assistant_v2.db", "home-assistant_v2.db-wal", "home-assistant_v2.db-shm"})
DENIED_PREFIXES = (".storage/", ".cloud/")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"[file-bridge] {message}", flush=True)


def git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=180)


def load_options() -> dict[str, Any]:
    defaults = {"poll_interval": 60, "config_repo": "", "control_branch": "bridge-control", "snapshot_branch": "live-snapshot", "status_branch": "bridge-status", "main_branch": "main"}
    if OPTIONS.is_file():
        payload = json.loads(OPTIONS.read_text())
        if isinstance(payload, dict): defaults.update(payload)
    if not isinstance(defaults["config_repo"], str) or not defaults["config_repo"]: raise ValueError("config_repo is required")
    return defaults


def safe_rel(path: str) -> PurePosixPath:
    if not isinstance(path, str): raise ValueError(f"unsafe path: {path!r}")
    value = PurePosixPath(path)
    if not path or value.is_absolute() or ".." in value.parts or str(value) == ".": raise ValueError(f"unsafe path: {path!r}")
    return value


def allowed_path(path: str) -> bool:
    normalized = str(safe_rel(path))
    if normalized in DENIED_EXACT or any(normalized == p[:-1] or normalized.startswith(p) for p in DENIED_PREFIXES): return False
    if normalized in ALLOWED_EXACT or normalized.startswith(ALLOWED_PREFIXES): return True
    parts = PurePosixPath(normalized).parts
    return len(parts) > 2 and parts[0] == "custom_components" and parts[1] in ALLOWED_COMPONENTS


def scope_for(path: str) -> str:
    first = safe_rel(path).parts[0]
    return {"configuration.yaml":"core", "automations.yaml":"automations", "scripts.yaml":"scripts", "scenes.yaml":"scenes", "go2rtc.yaml":"go2rtc", "www":"www"}.get(first, first)


def ensure_repo(options: dict[str, Any]) -> None:
    if not WORK.exists(): git(["clone", "--no-checkout", options["config_repo"], str(WORK)])
    git(["remote", "set-url", "origin", options["config_repo"]], cwd=WORK)
    git(["fetch", "--prune", "origin"], cwd=WORK)


def read_ref_file(ref: str, path: str) -> dict[str, Any] | None:
    result = subprocess.run(["git", "show", f"origin/{ref}:{path}"], cwd=WORK, capture_output=True, text=True, timeout=60)
    if result.returncode != 0: return None
    try: payload = json.loads(result.stdout)
    except json.JSONDecodeError: return None
    return payload if isinstance(payload, dict) else None


def checkout_branch(branch: str) -> None:
    exists = subprocess.run(["git", "show-ref", "--verify", f"refs/remotes/origin/{branch}"], cwd=WORK, capture_output=True).returncode == 0
    if exists: git(["checkout", "-B", branch, f"origin/{branch}"], cwd=WORK)
    else:
        git(["checkout", "--orphan", branch], cwd=WORK)
        for child in WORK.iterdir():
            if child.name != ".git": shutil.rmtree(child) if child.is_dir() else child.unlink()


def allowed_files(source: Path, scopes: set[str] | None = None) -> set[str]:
    result = set()
    for file in source.rglob("*"):
        if not file.is_file(): continue
        rel = file.relative_to(source).as_posix()
        if allowed_path(rel) and (scopes is None or scope_for(rel) in scopes): result.add(rel)
    return result


def copy_allowed(source: Path, destination: Path, scopes: set[str] | None = None) -> list[str]:
    copied = []
    for rel in sorted(allowed_files(source, scopes)):
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
        copied.append(rel)
    return copied


def remove_allowed_not_in_source(destination: Path, source: Path, scopes: set[str]) -> list[str]:
    source_files = allowed_files(source, scopes); removed = []
    for file in list(destination.rglob("*")):
        if not file.is_file(): continue
        rel = file.relative_to(destination).as_posix()
        if allowed_path(rel) and scope_for(rel) in scopes and rel not in source_files:
            file.unlink(); removed.append(rel)
    return sorted(removed)


def write_branch_file(branch: str, path: str, payload: dict[str, Any], message: str) -> None:
    checkout_branch(branch)
    target = WORK / safe_rel(path); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    git(["add", "--", path], cwd=WORK)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=WORK).returncode != 0:
        git(["-c", "user.name=ha-file-sync-bridge", "-c", "user.email=bridge@local", "commit", "-m", message], cwd=WORK)
        git(["push", "origin", f"HEAD:{branch}"], cwd=WORK)


def publish_status(options: dict[str, Any], entry: dict[str, Any]) -> None:
    entry["timestamp"] = now(); write_branch_file(options["status_branch"], STATUS_PATH, entry, f"bridge status: {entry.get('action','unknown')}")


def snapshot(options: dict[str, Any]) -> dict[str, Any]:
    checkout_branch(options["snapshot_branch"])
    for item in list(WORK.iterdir()):
        if item.name != ".git": shutil.rmtree(item) if item.is_dir() else item.unlink()
    copied = copy_allowed(CONFIG, WORK)
    metadata = {"action":"snapshot", "source":"home-assistant-config", "files":copied, "core_version":((CONFIG/".HA_VERSION").read_text().strip() if (CONFIG/".HA_VERSION").is_file() else None), "timestamp":now()}
    meta = WORK / META_PATH; meta.parent.mkdir(parents=True, exist_ok=True); meta.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n")
    git(["add", "--all"], cwd=WORK)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=WORK).returncode != 0:
        git(["-c", "user.name=ha-file-sync-bridge", "-c", "user.email=bridge@local", "commit", "-m", "live snapshot"], cwd=WORK); git(["push", "origin", f"HEAD:{options['snapshot_branch']}"], cwd=WORK)
    return metadata


def validate_request(request: dict[str, Any], options: dict[str, Any]) -> tuple[str, set[str]]:
    action = request.get("action")
    if action not in {"snapshot", "validate", "deploy", "rollback", "status"}: raise ValueError("unsupported action")
    raw_scopes = request.get("scope", [])
    if not isinstance(raw_scopes, list) or any(not isinstance(x, str) for x in raw_scopes): raise ValueError("scope must be a list of strings")
    scopes = set(raw_scopes)
    if scopes and not scopes <= ALLOWED_SCOPES: raise ValueError("invalid deploy scope")
    if action in {"validate", "deploy"}:
        sha = request.get("target_commit")
        if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()): raise ValueError("target_commit must be a full SHA")
        git(["merge-base", "--is-ancestor", sha, f"origin/{options['main_branch']}"], cwd=WORK)
    return action, scopes or set(ALLOWED_SCOPES)


def materialize(sha: str) -> Path:
    target = DATA / "candidate"; shutil.rmtree(target, ignore_errors=True); git(["worktree", "add", "--detach", str(target), sha], cwd=WORK); return target


def validate_candidate(candidate: Path, scopes: set[str]) -> list[str]:
    files = sorted(allowed_files(candidate, scopes))
    if not files: raise ValueError("no allowed files in requested scope")
    for rel in files:
        if rel.endswith((".yaml", ".yml")):
            subprocess.run(["python3", "-c", "import yaml,sys; yaml.safe_load(open(sys.argv[1], encoding='utf-8'))", str(candidate/rel)], check=True, capture_output=True, text=True, timeout=30)
    return files


def make_backup(scopes: set[str], files: set[str]) -> Path:
    backup = BACKUPS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"); backup.mkdir(parents=True); existing=[]
    for rel in sorted(files):
        source=CONFIG/rel
        if source.is_file():
            target=backup/rel; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source,target); existing.append(rel)
    (backup/"manifest.json").write_text(json.dumps({"files":sorted(files),"existing":existing,"scopes":sorted(scopes),"timestamp":now()},ensure_ascii=False,indent=2)+"\n")
    return backup


def restore_backup(backup: Path, files: set[str]) -> list[str]:
    restored=[]
    for rel in sorted(files):
        if not allowed_path(rel): raise ValueError(f"backup contains invalid path: {rel!r}")
        source=backup/rel; target=CONFIG/rel
        if source.is_file(): target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source,target); restored.append(rel)
        elif target.exists(): target.unlink()
    return restored


def supervisor_core_check() -> None:
    token = __import__("os").environ.get("SUPERVISOR_TOKEN")
    if not token: raise RuntimeError("SUPERVISOR_TOKEN is missing")
    request = urllib.request.Request(SUPERVISOR_CHECK_URL, method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"}, data=b"{}")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Supervisor core check failed: {exc}") from exc
    if not isinstance(body, dict) or body.get("result") != "ok": raise RuntimeError(f"Supervisor core check failed: {body!r}")


def deploy(sha: str, scopes: set[str]) -> dict[str, Any]:
    candidate=materialize(sha)
    try:
        candidate_files=set(validate_candidate(candidate,scopes)); current_files=allowed_files(CONFIG,scopes); backup_files=current_files|candidate_files; backup=make_backup(scopes,backup_files); removed=remove_allowed_not_in_source(CONFIG,candidate,scopes)
        for rel in sorted(candidate_files):
            target=CONFIG/rel; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(candidate/rel,target)
        try: supervisor_core_check()
        except Exception:
            restore_backup(backup,backup_files); raise
        LAST_GOOD.write_text(json.dumps({"commit":sha,"backup":str(backup),"files":sorted(backup_files),"scopes":sorted(scopes)},ensure_ascii=False,indent=2)+"\n")
        return {"action":"deploy","ok":True,"commit":sha,"files":sorted(candidate_files),"removed":removed}
    finally:
        subprocess.run(["git","worktree","remove","--force",str(candidate)],cwd=WORK,capture_output=True,timeout=60)


def rollback() -> dict[str, Any]:
    if not LAST_GOOD.is_file(): raise ValueError("no last-known-good deployment is available")
    previous=json.loads(LAST_GOOD.read_text()); backup=Path(previous["backup"]); files=previous.get("files",[])
    if not backup.is_dir() or not isinstance(files,list): raise ValueError("last-known-good backup is invalid")
    restore_files=set(files); current_files=allowed_files(CONFIG); safety_backup=make_backup(set(ALLOWED_SCOPES),current_files|restore_files)
    try:
        restored=restore_backup(backup,restore_files)
        try: supervisor_core_check()
        except Exception:
            restore_backup(safety_backup,current_files|restore_files); raise RuntimeError("rollback validation failed; pre-rollback state restored")
        return {"action":"rollback","ok":True,"commit":previous["commit"],"files":restored}
    finally: pass


def process(options: dict[str, Any]) -> None:
    ensure_repo(options); request=read_ref_file(options["control_branch"],REQUEST_PATH)
    if not request: return
    request_id=request.get("id")
    if not isinstance(request_id,str) or not request_id.strip(): raise ValueError("request id is required")
    last_id=LAST_REQUEST.read_text().strip() if LAST_REQUEST.exists() else ""
    if request_id==last_id: return
    try:
        action,scopes=validate_request(request,options)
        if action=="snapshot": result=snapshot(options)
        elif action=="deploy": result=deploy(request["target_commit"],scopes)
        elif action=="validate":
            candidate=materialize(request["target_commit"])
            try: result={"action":"validate","ok":True,"files":validate_candidate(candidate,scopes)}
            finally: subprocess.run(["git","worktree","remove","--force",str(candidate)],cwd=WORK,capture_output=True,timeout=60)
        elif action=="rollback": result=rollback()
        else: result={"action":"status","ok":True,"last_known_good":json.loads(LAST_GOOD.read_text()) if LAST_GOOD.exists() else None}
    except Exception as exc: result={"action":str(request.get("action","unknown")),"ok":False,"error":str(exc)}
    result["request_id"]=request_id
    try: publish_status(options,result)
    finally: LAST_REQUEST.write_text(request_id+"\n")


def main() -> None:
    while True:
        try: options=load_options(); process(options)
        except Exception as exc: log(f"request failed: {exc}")
        time.sleep(int(load_options()["poll_interval"]))


if __name__ == "__main__": main()
