#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

DATA = Path("/data")
CONFIG = Path("/config")
REPO = DATA / "ai-control-repo"
COMMANDS = REPO / ".ai-control" / "commands"
RESULTS = REPO / ".ai-control" / "results"
POLL = 15
MAX_READ_BYTES = 4 * 1024 * 1024

EXCLUDED_NAMES = {".git", ".storage", ".cloud", ".HA_VERSION", ".ssh", ".cache", ".ai-control", "secrets.yaml", "home-assistant_v2.db", "home-assistant_v2.db-shm", "home-assistant_v2.db-wal", "home-assistant_v2.db-journal", "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault"}
EXCLUDED_DIRS = {"tts", "media", "backups"}
EXCLUDED_SUFFIXES = {".passphrase", ".pem", ".key", ".p12", ".pfx"}
SENSITIVE_NAMES = {"secrets.yaml", ".env", ".env.local", ".env.production", ".env.development", "credentials.json", "credentials.yaml", "token.json", "service-account.json"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
CONFIG_SECRET_RE = re.compile(r'''(?im)^\s*(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|secret|token)\s*[:=]\s*["']([^"']{12,})["']\s*(?:#.*)?$''')


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message):
    print(f"[ai-control] {message}", flush=True)


def git(args, check=True):
    env = os.environ.copy()
    p = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=180, env=env)
    if check and p.returncode:
        text = (p.stderr or p.stdout).strip()
        raise RuntimeError(f"git failed ({p.returncode}): {text.splitlines()[-1] if text else 'unknown'}")
    return p


def excluded(name):
    return name in EXCLUDED_NAMES or name in EXCLUDED_DIRS or any(name.endswith(s) for s in EXCLUDED_SUFFIXES)


def safe_path(raw):
    raw = str(raw or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("path is required")
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("invalid relative path")
    if any(excluded(part) for part in p.parts):
        raise ValueError("target path is excluded")
    resolved = (CONFIG / p).resolve()
    root = CONFIG.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("target escapes /config")
    return resolved


def scan_bytes(path, data):
    rel = path.relative_to(CONFIG)
    if any(excluded(part) for part in rel.parts):
        return str(rel)
    if path.name in SENSITIVE_NAMES or any(path.name.endswith(s) for s in EXCLUDED_SUFFIXES):
        return str(rel)
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return None
    if any(rx.search(text) for rx in SECRET_PATTERNS):
        return str(rel)
    if path.suffix.lower() in {".yaml", ".yml", ".json", ".env", ".ini", ".conf", ".cfg", ".toml"}:
        for match in CONFIG_SECRET_RE.finditer(text):
            if match.group(1).strip().lower() not in {"changeme", "change-me", "your-token", "your_password", "placeholder", "example", "null", "none"}:
                return str(rel)
    return None


def read_op(body):
    target = safe_path(body.get("path"))
    if not target.exists() or not target.is_file():
        raise ValueError("file not found")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"file too large (max {MAX_READ_BYTES} bytes)")
    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    requested = str(body.get("encoding", "utf-8")).lower()
    if requested == "base64":
        return {"ok": True, "operation": "read", "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": digest, "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")}
    try:
        return {"ok": True, "operation": "read", "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": digest, "encoding": "utf-8", "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"ok": True, "operation": "read", "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": digest, "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")}


def write_op(body):
    target = safe_path(body.get("path"))
    if "content_base64" in body or str(body.get("encoding", "")).lower() == "base64":
        data = base64.b64decode(str(body.get("content_base64", body.get("content", ""))), validate=True)
    else:
        data = str(body.get("content", "")).encode("utf-8")
    finding = scan_bytes(target, data)
    if finding:
        raise ValueError(f"SECURITY BLOCKED: {finding}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".ai-tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return {"ok": True, "operation": "write", "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def browse_op(body):
    root = safe_path(body.get("path", "."))
    if not root.exists() or not root.is_dir():
        raise ValueError("directory not found")
    items = []
    for p in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        rel = p.relative_to(CONFIG)
        if any(excluded(part) for part in rel.parts):
            continue
        items.append({"name": p.name, "path": "/config/" + rel.as_posix(), "type": "directory" if p.is_dir() else "file", "bytes": p.stat().st_size if p.is_file() else None})
    return {"ok": True, "operation": "browse", "path": "/config/" + (root.relative_to(CONFIG).as_posix() if root != CONFIG else ""), "items": items}


def call_local_sync():
    req = urllib.request.Request("http://127.0.0.1:8099/sync", method="GET")
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def setup_repo():
    url = os.environ.get("HA_CONFIG_REPO", "git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git")
    branch = os.environ.get("HA_CONFIG_BRANCH", "main")
    REPO.parent.mkdir(parents=True, exist_ok=True)
    if not (REPO / ".git").is_dir():
        if REPO.exists():
            shutil.rmtree(REPO)
        subprocess.run(["git", "clone", "--no-checkout", url, str(REPO)], cwd=REPO.parent, check=True, timeout=180)
    git(["remote", "set-url", "origin", url])
    git(["fetch", "--prune", "origin"])
    git(["checkout", "-B", branch, f"origin/{branch}"])
    COMMANDS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    return branch


def publish_result(branch, command_path, result):
    result_path = RESULTS / command_path.name
    payload = {"id": command_path.stem, "completed_at": now(), **result}
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    git(["add", ".ai-control/results", ".ai-control/commands"])
    if git(["diff", "--cached", "--quiet"], check=False).returncode == 0:
        return
    git(["config", "user.name", "HA File Sync Bridge AI Control"])
    git(["config", "user.email", "ha-file-sync-bridge-ai@localhost"])
    git(["commit", "-m", f"AI control result {command_path.stem}"])
    git(["push", "origin", branch])


def process(branch, command_path):
    try:
        body = json.loads(command_path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            raise ValueError("command must be a JSON object")
        operation = str(body.get("operation", "")).lower().strip()
        if operation == "read":
            result = read_op(body)
        elif operation == "write":
            result = write_op(body)
            sync_result = call_local_sync()
            result["sync"] = sync_result
        elif operation == "browse":
            result = browse_op(body)
        elif operation == "sync":
            result = {"ok": True, "operation": "sync", "sync": call_local_sync()}
        else:
            raise ValueError("unsupported operation; use read, write, browse or sync")
        publish_result(branch, command_path, result)
        command_path.unlink(missing_ok=True)
        git(["add", ".ai-control/commands"])
        if git(["diff", "--cached", "--quiet"], check=False).returncode != 0:
            git(["commit", "-m", f"AI control completed {command_path.stem}"])
            git(["push", "origin", branch])
        log(f"completed {command_path.stem}: {operation}")
    except Exception as exc:
        try:
            publish_result(branch, command_path, {"ok": False, "error": str(exc)})
        finally:
            log(f"ERROR {command_path.stem}: {exc}")


def main():
    log("autonomous AI control channel starting")
    while True:
        try:
            branch = setup_repo()
            git(["pull", "--ff-only", "origin", branch])
            for command in sorted(COMMANDS.glob("*.json")):
                process(branch, command)
        except Exception as exc:
            log(f"loop error: {exc}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
