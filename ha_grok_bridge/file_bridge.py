#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.8"
DATA = Path("/data")
CONFIG = Path("/config")
WORK = DATA / "bridge-work"
STAGE = DATA / "deploy-stage"
SNAPSHOTS = DATA / "snapshots"
OPTIONS = DATA / "options.json"
STATE = DATA / "state.json"
STATUS = DATA / "status.json"
LOCK = DATA / "sync.lock"
PORT = 8099

DEFAULT_EXCLUDED_NAMES = {".git", ".storage", ".cloud", ".HA_VERSION", ".ssh", ".cache", "secrets.yaml", "home-assistant_v2.db", "home-assistant_v2.db-shm", "home-assistant_v2.db-wal", "home-assistant_v2.db-journal", "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault"}
DEFAULT_EXCLUDED_DIRS = {"tts", "media", "backups"}
DEFAULT_EXCLUDED_SUFFIXES = {".passphrase", ".pem", ".key", ".p12", ".pfx"}
SENSITIVE_NAMES = {"secrets.yaml", ".env", ".env.local", ".env.production", ".env.development", "credentials.json", "credentials.yaml", "token.json", "service-account.json", "ha-grok-bridge.passphrase", "ha-file-sync-bridge.passphrase"}
SECRET_PATTERNS = [re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"), re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"), re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"), re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")]
CONFIG_SECRET_RE = re.compile(r'''(?im)^\s*(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|secret|token)\s*[:=]\s*["']([^"']{12,})["']\s*(?:#.*)?$''')
SCANNABLE = {".yaml", ".yml", ".json", ".env", ".ini", ".conf", ".cfg", ".toml", ".txt", ".sh"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(s):
    print(f"[file-bridge] {s}", flush=True)


def load(p, default):
    try:
        v = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
        return v if isinstance(v, dict) else default
    except Exception:
        return default


def save(p, value):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def cfg():
    d = {"poll_interval": 60, "config_repo": "git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git", "branch": "main", "sync_mode": "bidirectional", "initial_sync": "ha_to_git", "dry_run": False, "max_snapshots": 10, "exclude_names": ",".join(sorted(DEFAULT_EXCLUDED_NAMES)), "exclude_dirs": ",".join(sorted(DEFAULT_EXCLUDED_DIRS)), "exclude_suffixes": ",".join(sorted(DEFAULT_EXCLUDED_SUFFIXES)), "secret_scan": True, "history_cleanup": False, "deploy_on_remote_change": True, "rollback_on_error": True}
    d.update(load(OPTIONS, {}))
    return d


def csv(value, fallback):
    return {x.strip() for x in value.split(",") if x.strip()} if isinstance(value, str) else set(fallback)


def excluded(name, c):
    return name in csv(c.get("exclude_names"), DEFAULT_EXCLUDED_NAMES) or name in csv(c.get("exclude_dirs"), DEFAULT_EXCLUDED_DIRS) or any(name.endswith(x) for x in csv(c.get("exclude_suffixes"), DEFAULT_EXCLUDED_SUFFIXES))


def ignored(path, c):
    return any(excluded(part, c) for part in path.parts)


def ignore(c):
    return lambda _d, names: {n for n in names if excluded(n, c)}


def git(args, cwd=WORK, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=180)
    if check and p.returncode:
        text = (p.stderr or p.stdout).strip()
        raise RuntimeError(f"git failed ({p.returncode}): {text.splitlines()[-1] if text else 'unknown'}")
    return p


def repo(url, branch):
    DATA.mkdir(parents=True, exist_ok=True)
    if not (WORK / ".git").is_dir():
        if WORK.exists():
            shutil.rmtree(WORK)
        git(["clone", "--no-checkout", url, str(WORK)], DATA)
    else:
        git(["remote", "set-url", "origin", url])
    git(["fetch", "--prune", "origin"])
    git(["checkout", "-B", branch, f"origin/{branch}"])


def remote_head(branch):
    return git(["rev-parse", f"origin/{branch}"]).stdout.strip()


def treehash(root, c):
    h = hashlib.sha256()
    if not root.is_dir():
        return h.hexdigest()
    for p in sorted(root.rglob("*")):
        r = p.relative_to(root)
        if p.is_file() and not ignored(r, c):
            h.update(r.as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def snapshot(c):
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    target = SNAPSHOTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copytree(CONFIG, target, ignore=ignore(c))
    limit = max(0, int(c.get("max_snapshots", 10)))
    items = sorted(p for p in SNAPSHOTS.iterdir() if p.is_dir())
    if limit and len(items) > limit:
        for p in items[:-limit]:
            shutil.rmtree(p, ignore_errors=True)
    return target


def restore_snapshot(path, c):
    if not path.is_dir():
        raise RuntimeError("snapshot not found")
    for p in list(CONFIG.iterdir()):
        if not excluded(p.name, c):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for p in path.iterdir():
        if excluded(p.name, c):
            continue
        d = CONFIG / p.name
        shutil.copytree(p, d, ignore=ignore(c)) if p.is_dir() else shutil.copy2(p, d)


def tracked_sensitive(c):
    out = []
    for item in git(["ls-files"]).stdout.splitlines():
        p = Path(item)
        if ignored(p, c):
            continue
        if any(part in SENSITIVE_NAMES or part.endswith(tuple(DEFAULT_EXCLUDED_SUFFIXES)) for part in p.parts):
            out.append(item)
    return out


def secret_scan(root, c):
    if not bool(c.get("secret_scan", True)):
        return []
    findings = []
    for p in root.rglob("*"):
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = p.relative_to(root)
        if ignored(rel, c):
            continue
        if p.name in SENSITIVE_NAMES or any(part.endswith(tuple(DEFAULT_EXCLUDED_SUFFIXES)) for part in rel.parts):
            findings.append(rel.as_posix())
            continue
        if p.suffix.lower() not in SCANNABLE:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if any(rx.search(text) for rx in SECRET_PATTERNS):
            findings.append(rel.as_posix())
            continue
        if p.suffix.lower() in {".yaml", ".yml", ".json", ".env", ".ini", ".conf", ".cfg", ".toml"}:
            for m in CONFIG_SECRET_RE.finditer(text):
                if m.group(1).strip().lower() not in {"changeme", "change-me", "your-token", "your_password", "placeholder", "example", "null", "none"}:
                    findings.append(rel.as_posix())
                    break
    return sorted(set(findings))


def prepare_from_config(c):
    if WORK.exists():
        for p in list(WORK.iterdir()):
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    count = 0
    for src in CONFIG.iterdir():
        if excluded(src.name, c):
            continue
        dst = WORK / src.name
        shutil.copytree(src, dst, ignore=ignore(c)) if src.is_dir() else shutil.copy2(src, dst)
        count += 1
    return count


def stage_remote(c):
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True, exist_ok=True)
    for src in WORK.iterdir():
        if src.name == ".git" or excluded(src.name, c):
            continue
        dst = STAGE / src.name
        shutil.copytree(src, dst, ignore=ignore(c)) if src.is_dir() else shutil.copy2(src, dst)
    findings = secret_scan(STAGE, c)
    if findings:
        raise RuntimeError(f"SECRET SCAN BLOCKED: {len(findings)} finding(s): {', '.join(findings[:5])}")


def deploy_stage(c, previous_snapshot=None):
    try:
        desired = {p.name for p in STAGE.iterdir() if not excluded(p.name, c)}
        for p in list(CONFIG.iterdir()):
            if not excluded(p.name, c) and p.name not in desired:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        for src in STAGE.iterdir():
            if excluded(src.name, c):
                continue
            dst = CONFIG / src.name
            if dst.exists():
                shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.copytree(src, dst, ignore=ignore(c)) if src.is_dir() else shutil.copy2(src, dst)
        if treehash(CONFIG, c) != treehash(STAGE, c):
            raise RuntimeError("deployment verification failed: /config hash mismatch")
    except Exception:
        if previous_snapshot is not None and bool(c.get("rollback_on_error", True)):
            log("deployment failed; rolling back snapshot")
            restore_snapshot(previous_snapshot, c)
        raise
    finally:
        if STAGE.exists():
            shutil.rmtree(STAGE, ignore_errors=True)


def push(branch, dry):
    git(["add", "-A"])
    changes = git(["diff", "--cached", "--name-status"]).stdout.splitlines()
    log(f"changes: {len(changes)}")
    if not changes:
        return False, git(["rev-parse", "HEAD"]).stdout.strip()
    if dry:
        git(["reset"])
        log("dry-run: commit/push skipped")
        return True, git(["rev-parse", "HEAD"]).stdout.strip()
    git(["config", "user.name", "HA File Sync Bridge"])
    git(["config", "user.email", "ha-file-sync-bridge@localhost"])
    git(["commit", "-m", f"Sync Home Assistant /config - {now()}"])
    head = git(["rev-parse", "HEAD"]).stdout.strip()
    git(["push", "origin", branch])
    git(["fetch", "--prune", "origin"])
    if remote_head(branch) != head:
        raise RuntimeError("push verification failed")
    log(f"push: OK ({head[:8]})")
    return True, head


def state():
    return load(STATE, {})


def set_status(**values):
    data = load(STATUS, {"version": VERSION})
    data.update(values)
    data.update(version=VERSION, updated_at=now())
    save(STATUS, data)


def acquire():
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.close(fd)
    except FileExistsError:
        raise RuntimeError("sync already running")


def release():
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


def deploy_remote(c, commit):
    log(f"deployment: preparing GitHub commit {commit[:8]}")
    previous = snapshot(c)
    stage_remote(c)
    set_status(state="deploying", deployment_commit=commit, error=None)
    deploy_stage(c, previous)
    return previous


def sync(c, forced=None):
    acquire()
    try:
        set_status(state="running", error=None)
        branch = str(c.get("branch", "main"))
        mode = forced or str(c.get("sync_mode", "bidirectional"))
        dry = bool(c.get("dry_run", False))
        log("sync start")
        repo(str(c["config_repo"]), branch)
        if git(["fsck", "--no-progress"], check=False).returncode:
            raise RuntimeError("repository invalid")
        log("repo: OK")
        s = state()
        last = s.get("last_sync_commit")
        local_changed = treehash(CONFIG, c) != s.get("last_config_hash")
        remote = remote_head(branch)
        remote_changed = bool(last and remote != last)
        if last and local_changed and remote_changed:
            raise RuntimeError("SYNC CONFLICT: both sides changed")
        if mode == "git_to_ha" or (mode == "bidirectional" and remote_changed and not local_changed and bool(c.get("deploy_on_remote_change", True))):
            deploy_remote(c, remote)
            log(f"GitHub -> /config: deployed {remote[:8]}")
        elif mode in {"ha_to_git", "bidirectional"} and (local_changed or not last):
            sensitive = tracked_sensitive(c)
            if sensitive:
                raise RuntimeError(f"SECURITY BLOCKED: tracked sensitive path: {sensitive[0]}")
            snapshot(c)
            count = prepare_from_config(c)
            log(f"/config prepared: {count} items")
            findings = secret_scan(WORK, c)
            if findings:
                raise RuntimeError(f"SECRET SCAN BLOCKED: {len(findings)} finding(s): {', '.join(findings[:5])}")
            _, head = push(branch, dry)
            remote = head if not dry else remote
        final = remote if remote else git(["rev-parse", "HEAD"]).stdout.strip()
        save(STATE, {**state(), "last_sync_commit": final, "last_config_hash": treehash(CONFIG, c), "last_success": now(), "last_deployment_commit": final})
        set_status(state="idle", last_sync=final, deployment_commit=final, error=None)
        log("sync complete")
    except Exception as e:
        set_status(state="error", error=str(e))
        log(f"ERROR: {e}")
    finally:
        release()


def restore(name, c, dry=False):
    target = SNAPSHOTS / name
    if not target.is_dir():
        raise RuntimeError("snapshot not found")
    if not dry:
        snapshot(c)
        restore_snapshot(target, c)
        if treehash(CONFIG, c) != treehash(target, c):
            raise RuntimeError("restore verification failed")
    log(f"snapshot restore {'planned' if dry else 'completed'}: {name}")


def safe_config_path(raw, c):
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("path is required")
    raw = raw.strip().replace("\\", "/")
    p = Path(raw)
    if p.is_absolute() or any(x in {"", "."} for x in p.parts) or ".." in p.parts:
        raise RuntimeError("invalid or unsafe path")
    if ignored(p, c):
        raise RuntimeError("target is excluded or sensitive")
    target = (CONFIG / p).resolve()
    root = CONFIG.resolve()
    if root not in target.parents:
        raise RuntimeError("target escapes /config")
    return target, p


def write_file(body, c):
    path = body.get("path")
    if not path:
        filename = body.get("filename")
        directory = str(body.get("directory", "")).strip("/\\")
        if not filename:
            raise RuntimeError("path or filename is required")
        path = f"{directory}/{filename}" if directory else filename
    target, rel = safe_config_path(path, c)
    if target.exists() and target.is_dir():
        raise RuntimeError("target is a directory")
    if "content_base64" in body:
        data = base64.b64decode(str(body["content_base64"]), validate=True)
    else:
        content = body.get("content", "")
        if not isinstance(content, str):
            raise RuntimeError("content must be a string")
        enc = str(body.get("encoding", "utf-8")).lower()
        data = base64.b64decode(content, validate=True) if enc in {"base64", "b64"} else content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    log(f"write: /config/{rel.as_posix()} ({len(data)} bytes)")
    return {"ok": True, "path": f"/config/{rel.as_posix()}", "bytes": len(data), "sha256": digest}


def browse(path, c):
    if path:
        target, rel = safe_config_path(path, c)
    else:
        target, rel = CONFIG, Path(".")
    if not target.is_dir():
        raise RuntimeError("path is not a directory")
    entries = []
    for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        r = p.relative_to(CONFIG)
        if ignored(r, c):
            continue
        entries.append({"name": p.name, "path": r.as_posix(), "type": "directory" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else None})
    return {"path": "/config" if rel.as_posix() == "." else f"/config/{rel.as_posix()}", "entries": entries}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def out(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            self.out(200, load(STATUS, {"version": VERSION}))
        elif parsed.path == "/snapshots":
            self.out(200, {"snapshots": sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir()) if SNAPSHOTS.exists() else []})
        elif parsed.path in {"/files", "/browse"}:
            try:
                q = parse_qs(parsed.query)
                self.out(200, browse(q.get("path", [""])[0], cfg()))
            except Exception as e:
                self.out(400, {"error": str(e)})
        elif parsed.path == "/":
            self.out(200, {"service": "HA File Sync Bridge", "version": VERSION, "status": load(STATUS, {})})
        else:
            self.out(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if self.path in {"/sync", "/sync/bidirectional", "/sync/ha_to_git", "/sync/git_to_ha"}:
                forced = "ha_to_git" if self.path.endswith("ha_to_git") else ("git_to_ha" if self.path.endswith("git_to_ha") else None)
                threading.Thread(target=lambda: sync(cfg(), forced), daemon=True).start()
                self.out(202, {"accepted": True})
            elif self.path == "/write":
                self.out(201, write_file(body, cfg()))
            elif self.path == "/restore":
                restore(str(body.get("snapshot", "")), cfg(), bool(body.get("dry_run", False)))
                self.out(200, {"ok": True})
            else:
                self.out(404, {"error": "not found"})
        except Exception as e:
            self.out(400, {"error": str(e)})


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    set_status(state="idle", error=None)
    log(f"HA File Sync Bridge {VERSION}")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def worker():
        first = True
        while True:
            try:
                c = cfg()
                forced = str(c.get("initial_sync", "ha_to_git")) if first and not state().get("last_sync_commit") else None
                sync(c, forced)
            except Exception as e:
                log(f"worker error: {e}")
            first = False
            time.sleep(max(10, int(cfg().get("poll_interval", 60))))

    threading.Thread(target=worker, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
