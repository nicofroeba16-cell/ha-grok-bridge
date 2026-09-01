#!/usr/bin/env python3
from __future__ import annotations
import hashlib, http.server, json, re, shutil, subprocess, threading, time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.2"
DATA = Path("/data")
CONFIG = Path("/config")
WORK = DATA / "bridge-work"
SNAPSHOTS = DATA / "snapshots"
OPTIONS = DATA / "options.json"
STATE = DATA / "state.json"
STATUS = DATA / "status.json"
LOCK = DATA / "sync.lock"
PORT = 8099

DEFAULT_EXCLUDED_NAMES = {
    ".git", ".storage", ".cloud", ".HA_VERSION", ".ssh", ".cache",
    "secrets.yaml", "home-assistant_v2.db", "home-assistant_v2.db-shm",
    "home-assistant_v2.db-wal", "home-assistant_v2.db-journal",
    "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault",
}
DEFAULT_EXCLUDED_DIRS = {"tts", "media", "backups"}
DEFAULT_EXCLUDED_SUFFIXES = {".passphrase", ".pem", ".key", ".p12", ".pfx"}

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"),
    re.compile(r"(?i)^\s*(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"(?i)^\s*(?:password|passwd|secret|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]\s*$"),
]
SCANNABLE = {".yaml", ".yml", ".json", ".env", ".ini", ".conf", ".cfg", ".toml", ".txt", ".sh"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    print(f"[file-bridge] {message}", flush=True)


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
        return value if isinstance(value, dict) else default
    except Exception:
        return default


def options() -> dict:
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
    }
    defaults.update(load_json(OPTIONS, {}))
    return defaults


def csv_set(value, fallback):
    return {x.strip() for x in value.split(",") if x.strip()} if isinstance(value, str) else set(fallback)


def excluded(name: str, cfg: dict) -> bool:
    return (
        name in csv_set(cfg.get("exclude_names"), DEFAULT_EXCLUDED_NAMES)
        or name in csv_set(cfg.get("exclude_dirs"), DEFAULT_EXCLUDED_DIRS)
        or any(name.endswith(s) for s in csv_set(cfg.get("exclude_suffixes"), DEFAULT_EXCLUDED_SUFFIXES))
    )


def ignored_tree(path: Path, cfg: dict) -> bool:
    return any(excluded(part, cfg) for part in path.parts)


def ignore_config(cfg: dict):
    return lambda _directory, names: {n for n in names if excluded(n, cfg)}


def run_git(args: list[str], cwd: Path = WORK, check: bool = True) -> subprocess.CompletedProcess[str]:
    log(f"git command: git {' '.join(args)}")
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=180)
    if proc.stdout.strip():
        log(f"git stdout: {proc.stdout.strip()}")
    if proc.stderr.strip():
        log(f"git stderr: {proc.stderr.strip()}")
    if check and proc.returncode:
        raise RuntimeError(f"git exited with {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def ensure_repo(url: str, branch: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not (WORK / ".git").is_dir():
        if WORK.exists():
            shutil.rmtree(WORK)
        run_git(["clone", "--no-checkout", url, str(WORK)], cwd=DATA)
        run_git(["checkout", "-B", branch, f"origin/{branch}"])
    else:
        run_git(["remote", "set-url", "origin", url])
        run_git(["fetch", "--prune", "origin"])
        run_git(["checkout", branch])


def validate_repo() -> None:
    if run_git(["fsck", "--no-progress"], check=False).returncode:
        raise RuntimeError("repository invalid")
    log("repository valid")
    log("repository access OK")


def tree_hash(root: Path, cfg: dict) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ignored_tree(rel, cfg):
            continue
        if path.is_file():
            digest.update(rel.as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def snapshot(cfg: dict) -> Path:
    if not CONFIG.is_dir():
        raise RuntimeError("/config is not mapped")
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    target = SNAPSHOTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copytree(CONFIG, target, ignore=ignore_config(cfg))
    limit = max(0, int(cfg.get("max_snapshots", 10)))
    snapshots = sorted(p for p in SNAPSHOTS.iterdir() if p.is_dir())
    if limit and len(snapshots) > limit:
        for old in snapshots[:-limit]:
            shutil.rmtree(old, ignore_errors=True)
    return target


def clear_allowed_worktree(cfg: dict) -> None:
    for path in list(WORK.iterdir()):
        if path.name == ".git" or excluded(path.name, cfg):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def copy_config_to_work(cfg: dict) -> int:
    if not CONFIG.is_dir():
        raise RuntimeError("/config is not mapped")
    clear_allowed_worktree(cfg)
    count = 0
    for source in CONFIG.iterdir():
        if excluded(source.name, cfg):
            continue
        destination = WORK / source.name
        if source.is_dir():
            shutil.copytree(source, destination, ignore=ignore_config(cfg))
        else:
            shutil.copy2(source, destination)
        count += 1
    return count


def tracked_sensitive_files(cfg: dict) -> list[str]:
    result = run_git(["ls-files"], check=True).stdout.splitlines()
    return [p for p in result if ignored_tree(Path(p), cfg)]


def secret_scan(cfg: dict) -> list[str]:
    if not bool(cfg.get("secret_scan", True)):
        return []
    findings = []
    for path in WORK.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ignored_tree(path.relative_to(WORK), cfg):
            continue
        if path.suffix.lower() not in SCANNABLE:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            findings.append(path.relative_to(WORK).as_posix())
    return findings


def commit_push(branch: str, dry_run: bool) -> bool:
    run_git(["add", "-A"])
    changes = run_git(["status", "--short"], check=False).stdout.strip()
    if not changes:
        log("no changes")
        return False
    log(f"staged changes detected: {len(changes.splitlines())}")
    staged = run_git(["diff", "--cached", "--name-status"], check=True).stdout.strip()
    if staged:
        log(f"staged files: {staged}")
    if dry_run:
        log("DRY-RUN: commit/push skipped")
        return True
    run_git(["config", "user.name", "HA File Sync Bridge"])
    run_git(["config", "user.email", "ha-file-sync-bridge@localhost"])
    run_git(["commit", "-m", f"Sync Home Assistant /config - {utc_now()}"])
    local_head = run_git(["rev-parse", "HEAD"], check=True).stdout.strip()
    log(f"local commit created: {local_head}")
    run_git(["push", "origin", branch])
    run_git(["fetch", "--prune", "origin"], check=True)
    remote_head = run_git(["rev-parse", f"origin/{branch}"], check=True).stdout.strip()
    if remote_head != local_head:
        raise RuntimeError(f"PUSH VERIFICATION FAILED: local {local_head} != origin/{branch} {remote_head}")
    log(f"push verified: origin/{branch} = {remote_head}")
    return True


def state() -> dict:
    return load_json(STATE, {})


def save_state(**values) -> None:
    current = state()
    current.update(values)
    atomic_json(STATE, current)


def copy_work_to_config(cfg: dict, dry_run: bool) -> int:
    if not CONFIG.is_dir():
        raise RuntimeError("/config is not mapped")
    allowed_remote = [p for p in WORK.iterdir() if p.name != ".git" and not excluded(p.name, cfg)]
    remote_names = {p.name for p in allowed_remote}
    count = 0
    for source in allowed_remote:
        destination = CONFIG / source.name
        if not dry_run:
            if destination.exists():
                shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
            if source.is_dir():
                shutil.copytree(source, destination, ignore=ignore_config(cfg))
            else:
                shutil.copy2(source, destination)
        count += 1
    if not dry_run:
        for destination in list(CONFIG.iterdir()):
            if not excluded(destination.name, cfg) and destination.name not in remote_names:
                shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
    return count


def sync_cycle(cfg: dict, forced_mode: str | None = None) -> None:
    if not CONFIG.is_dir():
        raise RuntimeError("/config is not mapped")
    branch = str(cfg.get("branch", "main"))
    dry = bool(cfg.get("dry_run", False))
    mode = forced_mode or str(cfg.get("sync_mode", "bidirectional"))
    ensure_repo(str(cfg["config_repo"]), branch)
    validate_repo()
    current = state()
    last_commit = current.get("last_sync_commit")
    local_changed = tree_hash(CONFIG, cfg) != current.get("last_config_hash")
    run_git(["fetch", "--prune", "origin"])
    remote_head = run_git(["rev-parse", f"origin/{branch}"]).stdout.strip()
    remote_changed = bool(last_commit and remote_head != last_commit)

    if mode == "git_to_ha":
        if last_commit and local_changed and remote_changed:
            raise RuntimeError("SYNC CONFLICT: /config and GitHub changed")
        run_git(["reset", "--hard", f"origin/{branch}"])
        snapshot(cfg)
        log(f"GitHub -> /config: {copy_work_to_config(cfg, dry)} items prepared")
    elif mode == "ha_to_git":
        sensitive = tracked_sensitive_files(cfg)
        if sensitive:
            raise RuntimeError("SECURITY BLOCKED: sensitive paths are already tracked: " + ", ".join(sensitive))
        snapshot(cfg)
        log(f"/config sync: {copy_config_to_work(cfg)} top-level items prepared")
        findings = secret_scan(cfg)
        if findings:
            raise RuntimeError("SECRET SCAN BLOCKED: " + ", ".join(findings))
        commit_push(branch, dry)
    else:
        if last_commit and local_changed and remote_changed:
            raise RuntimeError("SYNC CONFLICT: both /config and GitHub changed")
        if remote_changed and not local_changed:
            run_git(["reset", "--hard", f"origin/{branch}"])
            snapshot(cfg)
            log(f"GitHub -> /config: {copy_work_to_config(cfg, dry)} items prepared")
        elif local_changed or not last_commit:
            sensitive = tracked_sensitive_files(cfg)
            if sensitive:
                raise RuntimeError("SECURITY BLOCKED: sensitive paths are already tracked: " + ", ".join(sensitive))
            snapshot(cfg)
            log(f"/config sync: {copy_config_to_work(cfg)} top-level items prepared")
            findings = secret_scan(cfg)
            if findings:
                raise RuntimeError("SECRET SCAN BLOCKED: " + ", ".join(findings))
            commit_push(branch, dry)

    head = run_git(["rev-parse", "HEAD"]).stdout.strip()
    save_state(last_sync_commit=head, last_config_hash=tree_hash(CONFIG, cfg), last_success=utc_now())


def restore_snapshot(name: str, cfg: dict, dry_run: bool = False) -> None:
    source = SNAPSHOTS / name
    if not source.is_dir():
        raise RuntimeError("snapshot not found")
    if not dry_run:
        snapshot(cfg)
        for path in list(CONFIG.iterdir()):
            if not excluded(path.name, cfg):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        for path in source.iterdir():
            if excluded(path.name, cfg):
                continue
            destination = CONFIG / path.name
            if path.is_dir():
                shutil.copytree(path, destination, ignore=ignore_config(cfg))
            else:
                shutil.copy2(path, destination)
    log(f"snapshot restore {'planned' if dry_run else 'completed'}: {name}")


def history_cleanup(cfg: dict) -> None:
    if not bool(cfg.get("history_cleanup", False)):
        raise RuntimeError("history cleanup is disabled")
    raise RuntimeError("history cleanup requires an explicit repository-specific purge plan; refusing unsafe automatic rewrite")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, code: int, value: dict) -> None:
        raw = json.dumps(value, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/status":
            self.send_json(200, load_json(STATUS, {"version": VERSION}))
            return
        if self.path == "/snapshots":
            names = sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir()) if SNAPSHOTS.exists() else []
            self.send_json(200, {"snapshots": names})
            return
        if self.path == "/":
            status = load_json(STATUS, {"version": VERSION})
            names = sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir()) if SNAPSHOTS.exists() else []
            body = ("<!doctype html><html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                    f"<title>HA File Sync Bridge</title><body><h1>HA File Sync Bridge {VERSION}</h1>"
                    f"<pre>{json.dumps(status, indent=2)}</pre>"
                    "<form method='post' action='/sync/up'><button>HA → GitHub</button></form>"
                    "<form method='post' action='/sync/down'><button>GitHub → HA</button></form>"
                    f"<h2>Snapshots</h2><pre>{json.dumps(names, indent=2)}</pre></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        cfg = options()
        try:
            if self.path == "/sync/up":
                sync_cycle(cfg, "ha_to_git")
            elif self.path == "/sync/down":
                sync_cycle(cfg, "git_to_ha")
            elif self.path.startswith("/restore/"):
                restore_snapshot(self.path.split("/", 2)[2], cfg, bool(cfg.get("dry_run")))
            elif self.path == "/history-cleanup":
                history_cleanup(cfg)
            else:
                self.send_json(404, {"error": "not found"})
                return
            self.send_json(200, {"ok": True})
        except Exception as exc:
            self.send_json(409, {"ok": False, "error": str(exc)})


def serve() -> None:
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def main() -> None:
    log(f"HA File Sync Bridge {VERSION}")
    threading.Thread(target=serve, daemon=True).start()
    while True:
        result = "ok"
        error = ""
        try:
            cfg = options()
            with LOCK.open("x"):
                if not state().get("initialized"):
                    sync_cycle(cfg, str(cfg.get("initial_sync", "ha_to_git")))
                    save_state(initialized=True)
                else:
                    sync_cycle(cfg)
        except FileExistsError:
            result = "locked"
            error = "another sync is running"
        except Exception as exc:
            result = "error"
            error = str(exc)
            log(f"REQUEST FAILED: {exc}")
        finally:
            try:
                LOCK.unlink()
            except FileNotFoundError:
                pass
            cfg = options()
            atomic_json(STATUS, {
                "version": VERSION,
                "timestamp": utc_now(),
                "result": result,
                "error": error,
                "repo": cfg.get("config_repo"),
                "branch": cfg.get("branch"),
                "config_hash": tree_hash(CONFIG, cfg) if CONFIG.is_dir() else None,
                "last_sync_commit": state().get("last_sync_commit"),
                "last_success": state().get("last_success"),
            })
        time.sleep(max(5, int(options().get("poll_interval", 60))))


if __name__ == "__main__":
    main()
