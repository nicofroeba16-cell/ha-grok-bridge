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

VERSION = "1.12"
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
CONTROL_DIR = ".ai-control"
CONTROL_COMMANDS = Path(CONTROL_DIR) / "commands"
CONTROL_RESULTS = Path(CONTROL_DIR) / "results"
MAX_CONTROL_COMMANDS = 10

DEFAULT_EXCLUDED_NAMES = {".git", ".storage", ".cloud", ".HA_VERSION", ".ssh", ".cache", ".ai-control", "secrets.yaml", "home-assistant_v2.db", "home-assistant_v2.db-shm", "home-assistant_v2.db-wal", "home-assistant_v2.db-journal", "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault"}
DEFAULT_EXCLUDED_DIRS = {"tts", "media", "backups"}
DEFAULT_EXCLUDED_SUFFIXES = {".passphrase", ".pem", ".key", ".p12", ".pfx"}
SENSITIVE_NAMES = {"secrets.yaml", ".env", ".env.local", ".env.production", ".env.development", "credentials.json", "credentials.yaml", "token.json", "service-account.json", "ha-grok-bridge.passphrase", "ha-file-sync-bridge.passphrase"}
SECRET_PATTERNS = [re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"), re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"), re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"), re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")]
CONFIG_SECRET_RE = re.compile(r'''(?im)^\s*(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|secret|token)\s*[:=]\s*["']([^"']{12,})["']\s*(?:#.*)?$''')
SCANNABLE = {".yaml", ".yml", ".json", ".env", ".ini", ".conf", ".cfg", ".toml", ".txt", ".sh"}
MAX_READ_BYTES = 4 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message):
    print(f"[file-bridge] {message}", flush=True)


def load(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
        return value if isinstance(value, dict) else default
    except Exception:
        return default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def cfg():
    defaults = {
        "poll_interval": 60,
        "config_repo": "git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git",
        "branch": "main",
        "sync_mode": "bidirectional",
        "initial_sync": "ha_to_git",
        "dry_run": False,
        "max_snapshots": 10,
        "exclude_names": ",".join(sorted(DEFAULT_EXCLUDED_NAMES)),
        "exclude_dirs": ",".join(sorted(DEFAULT_EXCLUDED_DIRS)),
        "exclude_suffixes": ",".join(sorted(DEFAULT_EXCLUDED_SUFFIXES)),
        "secret_scan": True,
        "history_cleanup": False,
        "deploy_on_remote_change": True,
        "rollback_on_error": True,
        "auto_reload": False,
    }
    defaults.update(load(OPTIONS, {}))
    return defaults


def csv(value, fallback):
    return {x.strip() for x in value.split(",") if x.strip()} if isinstance(value, str) else set(fallback)


def excluded(name, c):
    return name in csv(c.get("exclude_names"), DEFAULT_EXCLUDED_NAMES) or name in csv(c.get("exclude_dirs"), DEFAULT_EXCLUDED_DIRS) or any(name.endswith(s) for s in csv(c.get("exclude_suffixes"), DEFAULT_EXCLUDED_SUFFIXES))


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
        rel = p.relative_to(root)
        if p.is_file() and not ignored(rel, c):
            h.update(rel.as_posix().encode() + b"\0" + p.read_bytes())
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
        dst = CONFIG / p.name
        shutil.copytree(p, dst, ignore=ignore(c)) if p.is_dir() else shutil.copy2(p, dst)


def tracked_sensitive(c):
    result = []
    for item in git(["ls-files"]).stdout.splitlines():
        rel = Path(item)
        if ignored(rel, c):
            continue
        if any(part in SENSITIVE_NAMES or part.endswith(tuple(csv(c.get("exclude_suffixes"), DEFAULT_EXCLUDED_SUFFIXES))) for part in rel.parts):
            result.append(item)
    return result


def tracked_excluded(c):
    return [item for item in git(["ls-files"]).stdout.splitlines() if ignored(Path(item), c)]


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
        if p.name in SENSITIVE_NAMES or any(part.endswith(tuple(csv(c.get("exclude_suffixes"), DEFAULT_EXCLUDED_SUFFIXES))) for part in rel.parts):
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
            for match in CONFIG_SECRET_RE.finditer(text):
                if match.group(1).strip().lower() not in {"changeme", "change-me", "your-token", "your_password", "placeholder", "example", "null", "none"}:
                    findings.append(rel.as_posix())
                    break
    return sorted(set(findings))


def prepare_from_config(c):
    control=WORK / CONTROL_DIR
    preserve=DATA / "control-preserve"
    if preserve.exists(): shutil.rmtree(preserve, ignore_errors=True)
    if control.exists(): shutil.copytree(control,preserve)
    if WORK.exists():
        for item in list(WORK.iterdir()):
            if item.name not in {".git",CONTROL_DIR}:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
    count=0
    for src in CONFIG.iterdir():
        if excluded(src.name,c): continue
        dst=WORK/src.name
        shutil.copytree(src,dst,ignore=ignore(c)) if src.is_dir() else shutil.copy2(src,dst)
        count+=1
    if preserve.exists():
        if control.exists(): shutil.rmtree(control)
        shutil.copytree(preserve,control)
        shutil.rmtree(preserve,ignore_errors=True)
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


def push(branch, dry, c):
    tracked = tracked_excluded(c)
    if tracked:
        log(f"excluded cleanup: {len(tracked)} tracked runtime path(s) excluded")
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


def safe_config_path(raw_path, c):
    raw = str(raw_path or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("path is required")
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("invalid relative path")
    if ignored(candidate, c):
        raise ValueError("target path is excluded")
    resolved = (CONFIG / candidate).resolve()
    root = CONFIG.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("target escapes /config")
    return resolved


def write_file(body, c):
    path = body.get("path")
    if not path and body.get("directory") is not None:
        directory = str(body.get("directory") or "").strip("/")
        filename = str(body.get("filename") or "").strip()
        path = f"{directory}/{filename}" if directory else filename
    target = safe_config_path(path, c)
    target.parent.mkdir(parents=True, exist_ok=True)
    if "content_base64" in body or str(body.get("encoding", "")).lower() == "base64":
        data = base64.b64decode(str(body.get("content_base64", body.get("content", ""))))
    else:
        data = str(body.get("content", "")).encode("utf-8")
    target.write_bytes(data)
    return {"ok": True, "path": "/config/" + target.relative_to(CONFIG).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def read_file(path_value, c, encoding="utf-8"):
    target = safe_config_path(path_value, c)
    if not target.exists() or not target.is_file():
        raise ValueError("file not found")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"file too large to read via API (max {MAX_READ_BYTES} bytes)")
    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    rel = "/config/" + target.relative_to(CONFIG).as_posix()
    if encoding.lower() == "base64":
        return {"ok": True, "path": rel, "bytes": len(data), "sha256": digest, "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")}
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": True, "path": rel, "bytes": len(data), "sha256": digest, "encoding": "base64", "content_base64": base64.b64encode(data).decode("ascii")}
    return {"ok": True, "path": rel, "bytes": len(data), "sha256": digest, "encoding": "utf-8", "content": content}


def browse(path_value, c):
    root = safe_config_path(path_value or ".", c)
    if not root.exists() or not root.is_dir():
        raise ValueError("directory not found")
    items = []
    for p in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        rel = p.relative_to(CONFIG)
        if ignored(rel, c):
            continue
        items.append({"name": p.name, "path": "/config/" + rel.as_posix(), "type": "directory" if p.is_dir() else "file", "bytes": p.stat().st_size if p.is_file() else None})
    return {"ok": True, "path": "/config/" + (root.relative_to(CONFIG).as_posix() if root != CONFIG else ""), "items": items}


def deploy_remote(c, commit):
    log(f"deployment: preparing GitHub commit {commit[:8]}")
    previous = snapshot(c)
    stage_remote(c)
    set_status(state="deploying", deployment_commit=commit, error=None)
    deploy_stage(c, previous)
    return previous


def remote_config_changed(last,remote,c):
    if not last or not remote or last==remote: return False
    names=git(["diff","--name-only",last,remote],check=False).stdout.splitlines()
    return any(not ignored(Path(n),c) for n in names)

def _control_result(results_dir,command_id,result):
    results_dir.mkdir(parents=True,exist_ok=True)
    safe=re.sub(r"[^A-Za-z0-9._-]","_",str(command_id)) or "command"
    save(results_dir/f"{safe}.json",{"id":str(command_id),"completed_at":now(),**result})

def _control_read(path_value,c,encoding="utf-8"):
    target=safe_config_path(path_value,c)
    if not target.exists() or not target.is_file(): raise ValueError("file not found")
    data=target.read_bytes()
    if len(data)>MAX_READ_BYTES: raise ValueError(f"file too large to read via control queue (max {MAX_READ_BYTES} bytes)")
    scan_dir=DATA/"control-read-scan"
    shutil.rmtree(scan_dir,ignore_errors=True); scan_dir.mkdir(parents=True,exist_ok=True)
    scan_file=scan_dir/(target.name or "read.txt"); scan_file.write_bytes(data)
    findings=secret_scan(scan_dir,c); shutil.rmtree(scan_dir,ignore_errors=True)
    digest=hashlib.sha256(data).hexdigest(); rel="/config/"+target.relative_to(CONFIG).as_posix()
    if findings: return {"ok":False,"path":rel,"bytes":len(data),"sha256":digest,"error":"SECURITY BLOCKED: read result contains secret-like material"}
    if encoding.lower()=="base64": return {"ok":True,"path":rel,"bytes":len(data),"sha256":digest,"encoding":"base64","content_base64":base64.b64encode(data).decode("ascii")}
    try: return {"ok":True,"path":rel,"bytes":len(data),"sha256":digest,"encoding":"utf-8","content":data.decode("utf-8")}
    except UnicodeDecodeError: return {"ok":True,"path":rel,"bytes":len(data),"sha256":digest,"encoding":"base64","content_base64":base64.b64encode(data).decode("ascii")}

def process_ai_control(c,branch,dry):
    commands_dir=WORK/CONTROL_COMMANDS; results_dir=WORK/CONTROL_RESULTS
    if not commands_dir.is_dir(): return
    for command_path in sorted(commands_dir.glob("*.json"))[:MAX_CONTROL_COMMANDS]:
        command_id=command_path.stem
        try:
            command=json.loads(command_path.read_text(encoding="utf-8"))
            if not isinstance(command,dict): raise ValueError("command must be a JSON object")
            action=str(command.get("action","")).strip().lower()
            if action not in {"read","write","browse","sync"}: raise ValueError("unsupported action")
            previous = snapshot(c) if action == "write" else None
            if action=="read": result=_control_read(command.get("path",""),c,str(command.get("encoding","utf-8")))
            elif action=="write":
                result=write_file(command,c)
                prepare_from_config(c)
                findings=secret_scan(WORK,c)
                if findings:
                    if previous is not None and bool(c.get("rollback_on_error", True)): restore_snapshot(previous,c)
                    raise RuntimeError(f"SECRET SCAN BLOCKED: {len(findings)} finding(s): {', '.join(findings[:5])}")
            elif action=="browse": result=browse(command.get("path","."),c)
            else: result={"ok":True,"commit":remote_head(branch),"config_hash":treehash(CONFIG,c)}
            command_path.unlink(missing_ok=True); _control_result(results_dir,command_id,{"action":action,**result})
            push(branch,dry,c,message=f"AI control: {action} {command_id}")
            log(f"AI control: completed {action} {command_id}")
        except Exception as exc:
            try:
                command_path.unlink(missing_ok=True); _control_result(results_dir,command_id,{"ok":False,"error":str(exc)})
                push(branch,dry,c,message=f"AI control: failed {command_id}")
            except Exception as result_exc: log(f"AI control result failed: {result_exc}")
            log(f"AI control ERROR {command_id}: {exc}")

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
        local_hash = treehash(CONFIG, c)
        local_changed = bool(last) and local_hash != s.get("last_config_hash")
        remote = remote_head(branch)
        remote_changed = bool(last) and remote != last
        remote_config_change = remote_config_changed(last,remote,c)
        if last and local_changed and remote_config_change:
            raise RuntimeError("SYNC CONFLICT: both sides changed")
        if mode == "git_to_ha" or (mode == "bidirectional" and remote_config_change and not local_changed and bool(c.get("deploy_on_remote_change", True))):
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
            _, head = push(branch, dry, c)
            remote = head if not dry else remote
        process_ai_control(c,branch,dry)
        remote=remote_head(branch)
        final = remote if remote else git(["rev-parse", "HEAD"]).stdout.strip()
        save(STATE, {**state(), "last_sync_commit": final, "last_config_hash": treehash(CONFIG, c), "last_success": now(), "last_deployment_commit": final})
        set_status(state="idle", last_sync=final, deployment_commit=final, error=None)
        log("sync complete")
        return {"ok": True, "commit": final}
    except Exception as exc:
        set_status(state="error", error=str(exc))
        log(f"ERROR: {exc}")
        return {"ok": False, "error": str(exc)}
    finally:
        release()


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        c = cfg()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in {"/", "/status"}:
                self._json(200, load(STATUS, {"version": VERSION, "state": "unknown"}))
            elif parsed.path in {"/files", "/browse"}:
                self._json(200, browse(query.get("path", ["."])[0], c))
            elif parsed.path == "/read":
                path = query.get("path", [""])[0]
                encoding = query.get("encoding", ["utf-8"])[0]
                self._json(200, read_file(path, c, encoding))
            elif parsed.path == "/state":
                self._json(200, {"ok": True, **state()})
            elif parsed.path == "/sync":
                self._json(200, sync(c))
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def do_POST(self):
        c = cfg()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path == "/write":
                self._json(200, write_file(body, c))
            elif self.path == "/sync":
                self._json(200, sync(c))
            elif self.path == "/deploy":
                self._json(200, sync(c, "git_to_ha"))
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        log(fmt % args)


def loop():
    while True:
        c = cfg()
        sync(c)
        time.sleep(max(5, int(c.get("poll_interval", 60))))


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG.mkdir(parents=True, exist_ok=True)
    log(f"HA File Sync Bridge {VERSION}")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=loop, daemon=True).start()
    log(f"HTTP API listening on {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
