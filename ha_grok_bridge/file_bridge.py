#!/usr/bin/env python3
from __future__ import annotations

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
from typing import Any

VERSION = "1.0.0"
DATA = Path("/data")
CONFIG = Path("/config")
WORK = DATA / "bridge-work"
SNAPSHOTS = DATA / "snapshots"
OPTIONS = DATA / "options.json"
STATE = DATA / "state.json"
STATUS = DATA / "status.json"
LOCK = DATA / "sync.lock"
PORT = 8099
DEFAULT_EXCLUDED_NAMES = {".git", ".storage", ".cloud", ".HA_VERSION", ".ssh", ".cache", "secrets.yaml", "home-assistant_v2.db", "home-assistant_v2.db-shm", "home-assistant_v2.db-wal", "home-assistant_v2.db-journal", "home-assistant.log", "home-assistant.log.1", "home-assistant.log.fault"}
DEFAULT_EXCLUDED_DIRS = {"tts", "media", "backups"}
DEFAULT_EXCLUDED_SUFFIXES = {".passphrase", ".pem", ".key", ".p12", ".pfx"}
SECRET_PATTERNS = [re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"), re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]"), re.compile(r"(?i)\b(password|passwd|token|secret)\s*[:=]\s*['\"][^'\"]+['\"]")]

def utc_now() -> str: return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
def log(message: str) -> None: print(f"[file-bridge] {message}", flush=True)
def atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp"); tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8"); tmp.replace(path)
def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
        return value if isinstance(value, dict) else default
    except Exception: return default

def options() -> dict[str, Any]:
    cfg: dict[str, Any] = {"poll_interval":60,"config_repo":"git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git","branch":"main","sync_mode":"bidirectional","initial_sync":"ha_to_git","dry_run":False,"max_snapshots":10,"exclude_names":",".join(sorted(DEFAULT_EXCLUDED_NAMES)),"exclude_dirs":",".join(sorted(DEFAULT_EXCLUDED_DIRS)),"exclude_suffixes":",".join(sorted(DEFAULT_EXCLUDED_SUFFIXES)),"secret_scan":True,"history_cleanup":False}
    cfg.update(load_json(OPTIONS, {})); return cfg

def csv_set(value: Any, fallback: set[str]) -> set[str]:
    return {x.strip() for x in value.split(",") if x.strip()} if isinstance(value,str) else fallback

def excluded(name: str, cfg: dict[str, Any]) -> bool:
    return name in csv_set(cfg.get("exclude_names"),DEFAULT_EXCLUDED_NAMES) or name in csv_set(cfg.get("exclude_dirs"),DEFAULT_EXCLUDED_DIRS) or any(name.endswith(s) for s in csv_set(cfg.get("exclude_suffixes"),DEFAULT_EXCLUDED_SUFFIXES))
def ignore_config(cfg: dict[str, Any]):
    return lambda _directory,names:{n for n in names if excluded(n,cfg)}

def run_git(args:list[str],cwd:Path=WORK,check:bool=True)->subprocess.CompletedProcess[str]:
    log(f"git command: git {' '.join(args)}"); p=subprocess.run(["git",*args],cwd=cwd,check=False,capture_output=True,text=True,timeout=180)
    if p.stdout.strip(): log(f"git stdout: {p.stdout.strip()}")
    if p.stderr.strip(): log(f"git stderr: {p.stderr.strip()}")
    if check and p.returncode: raise RuntimeError(f"git exited with {p.returncode}: {p.stderr.strip() or p.stdout.strip()}")
    return p

def ensure_repo(url:str,branch:str)->None:
    DATA.mkdir(parents=True,exist_ok=True)
    if not (WORK/".git").is_dir():
        if WORK.exists(): shutil.rmtree(WORK)
        run_git(["clone","--no-checkout",url,str(WORK)],cwd=DATA); run_git(["checkout","-B",branch,f"origin/{branch}"])
    else:
        run_git(["remote","set-url","origin",url]); run_git(["fetch","--prune","origin"]); run_git(["checkout",branch])

def validate_repo()->None:
    p=run_git(["fsck","--no-progress"],check=False)
    if p.returncode: raise RuntimeError("repository invalid")
    log("repository valid"); log("repository access OK")

def tree_hash(root:Path,cfg:dict[str,Any])->str:
    h=hashlib.sha256()
    if not root.is_dir(): return h.hexdigest()
    for path in sorted(root.rglob("*")):
        rel=path.relative_to(root).as_posix()
        if any(excluded(part,cfg) for part in Path(rel).parts): continue
        if path.is_file(): h.update(rel.encode()); h.update(b"\0"); h.update(path.read_bytes())
    return h.hexdigest()

def snapshot(cfg:dict[str,Any])->Path:
    if not CONFIG.is_dir(): raise RuntimeError("/config is not mapped")
    SNAPSHOTS.mkdir(parents=True,exist_ok=True); target=SNAPSHOTS/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); shutil.copytree(CONFIG,target,ignore=ignore_config(cfg))
    limit=max(0,int(cfg.get("max_snapshots",10))); snapshots=sorted([p for p in SNAPSHOTS.iterdir() if p.is_dir()])
    if limit and len(snapshots)>limit:
        for old in snapshots[:-limit]: shutil.rmtree(old,ignore_errors=True)
    return target

def clear_worktree_except_git()->None:
    for child in WORK.iterdir():
        if child.name!=".git": shutil.rmtree(child) if child.is_dir() else child.unlink()

def copy_config_to_work(cfg:dict[str,Any])->int:
    if not CONFIG.is_dir(): raise RuntimeError("/config is not mapped")
    clear_worktree_except_git(); count=0
    for src in CONFIG.iterdir():
        if excluded(src.name,cfg): continue
        dst=WORK/src.name; shutil.copytree(src,dst,ignore=ignore_config(cfg)) if src.is_dir() else shutil.copy2(src,dst); count+=1
    return count

def copy_work_to_config(cfg:dict[str,Any],dry_run:bool=False)->int:
    if not CONFIG.is_dir(): raise RuntimeError("/config is not mapped")
    remote_names={p.name for p in WORK.iterdir() if p.name!=".git"}; count=0
    for src in WORK.iterdir():
        if src.name==".git" or excluded(src.name,cfg): continue
        dst=CONFIG/src.name
        if not dry_run:
            if dst.exists(): shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.copytree(src,dst,ignore=ignore_config(cfg)) if src.is_dir() else shutil.copy2(src,dst)
        count+=1
    for dst in CONFIG.iterdir():
        if excluded(dst.name,cfg) or dst.name in remote_names: continue
        if not dry_run: shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    return count

def secret_scan(cfg:dict[str,Any])->list[str]:
    if not bool(cfg.get("secret_scan",True)): return []
    findings=[]
    for path in WORK.rglob("*"):
        if not path.is_file() or ".git" in path.parts or excluded(path.name,cfg): continue
        try: text=path.read_text(encoding="utf-8",errors="ignore")
        except Exception: continue
        if any(p.search(text) for p in SECRET_PATTERNS): findings.append(path.relative_to(WORK).as_posix())
    return findings

def commit_and_push(branch:str,dry_run:bool)->bool:
    run_git(["add","-A"]); changes=run_git(["status","--short"],check=False).stdout.strip()
    if not changes: log("no changes"); return False
    if dry_run: log("DRY-RUN: commit/push skipped"); return True
    run_git(["config","user.name","HA File Sync Bridge"]); run_git(["config","user.email","ha-file-sync-bridge@localhost"]); run_git(["commit","-m",f"Sync Home Assistant /config - {utc_now()}"]); run_git(["push","origin",branch]); return True

def state()->dict[str,Any]: return load_json(STATE,{})
def save_state(**kwargs:Any)->None: s=state(); s.update(kwargs); atomic_json(STATE,s)

def sync_cycle(cfg:dict[str,Any],force_mode:str|None=None)->None:
    if not CONFIG.is_dir(): raise RuntimeError("/config is not mapped")
    branch=str(cfg.get("branch","main")); dry=bool(cfg.get("dry_run",False)); mode=force_mode or str(cfg.get("sync_mode","bidirectional")); ensure_repo(str(cfg["config_repo"]),branch); validate_repo()
    current_head=run_git(["rev-parse","HEAD"]).stdout.strip(); last=state().get("last_sync_commit"); local_changed=tree_hash(CONFIG,cfg)!=state().get("last_config_hash")
    run_git(["fetch","--prune","origin"]); remote_head=run_git(["rev-parse",f"origin/{branch}"]).stdout.strip(); remote_changed=bool(last and remote_head!=last)
    if mode=="git_to_ha":
        if last and local_changed and remote_changed: raise RuntimeError("SYNC CONFLICT: /config and GitHub changed")
        run_git(["reset","--hard",f"origin/{branch}"]); snapshot(cfg); n=copy_work_to_config(cfg,dry); log(f"GitHub -> /config: {n} items prepared")
    elif mode=="ha_to_git":
        snapshot(cfg); n=copy_config_to_work(cfg); log(f"/config sync: {n} top-level items prepared"); findings=secret_scan(cfg)
        if findings: raise RuntimeError("SECRET SCAN BLOCKED: "+", ".join(findings))
        commit_and_push(branch,dry)
    else:
        if last and local_changed and remote_changed: raise RuntimeError("SYNC CONFLICT: both /config and GitHub changed")
        if remote_changed and not local_changed:
            run_git(["reset","--hard",f"origin/{branch}"]); snapshot(cfg); n=copy_work_to_config(cfg,dry); log(f"GitHub -> /config: {n} items prepared")
        elif local_changed or not last:
            snapshot(cfg); n=copy_config_to_work(cfg); log(f"/config sync: {n} top-level items prepared"); findings=secret_scan(cfg)
            if findings: raise RuntimeError("SECRET SCAN BLOCKED: "+", ".join(findings))
            commit_and_push(branch,dry)
    save_state(last_sync_commit=run_git(["rev-parse","HEAD"]).stdout.strip(),last_config_hash=tree_hash(CONFIG,cfg))

def restore_snapshot(name:str,cfg:dict[str,Any],dry_run:bool=False)->None:
    src=SNAPSHOTS/name
    if not src.is_dir(): raise RuntimeError("snapshot not found")
    if not dry_run: snapshot(cfg)
    for p in list(CONFIG.iterdir()):
        if not excluded(p.name,cfg) and not dry_run: shutil.rmtree(p) if p.is_dir() else p.unlink()
    if not dry_run:
        for p in src.iterdir():
            if excluded(p.name,cfg): continue
            dst=CONFIG/p.name; shutil.copytree(p,dst,ignore=ignore_config(cfg)) if p.is_dir() else shutil.copy2(p,dst)
    log(f"snapshot restore {'planned' if dry_run else 'completed'}: {name}")

def history_cleanup(cfg:dict[str,Any])->None:
    if not bool(cfg.get("history_cleanup",False)): raise RuntimeError("history cleanup is disabled")
    paths=csv_set(cfg.get("exclude_names"),DEFAULT_EXCLUDED_NAMES)|csv_set(cfg.get("exclude_dirs"),DEFAULT_EXCLUDED_DIRS)|{"*.passphrase","*.pem","*.key","*.p12","*.pfx"}
    script="git rm -r --cached --ignore-unmatch "+" ".join(sorted(paths)); run_git(["filter-branch","--force","--index-filter",script,"--prune-empty","--tag-name-filter","cat","--","--all"]); run_git(["reflog","expire","--expire=now","--all"]); run_git(["gc","--prune=now","--aggressive"]); run_git(["push","--force","--all","origin"]); run_git(["push","--force","--tags","origin"],check=False); log("history cleanup completed")

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*_:Any)->None: pass
    def send_json(self,code:int,value:dict[str,Any])->None:
        raw=json.dumps(value,indent=2).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self)->None:
        if self.path=="/status": self.send_json(200,load_json(STATUS,{"version":VERSION})); return
        if self.path=="/snapshots": self.send_json(200,{"snapshots":[p.name for p in sorted(SNAPSHOTS.iterdir()) if p.is_dir()]}); return
        self.send_json(404,{"error":"not found"})
    def do_POST(self)->None:
        cfg=options()
        try:
            if self.path=="/sync/up": sync_cycle(cfg,"ha_to_git")
            elif self.path=="/sync/down": sync_cycle(cfg,"git_to_ha")
            elif self.path.startswith("/restore/"): restore_snapshot(self.path.split("/",2)[2],cfg,bool(cfg.get("dry_run")))
            elif self.path=="/history-cleanup": history_cleanup(cfg)
            else: self.send_json(404,{"error":"not found"}); return
            self.send_json(200,{"ok":True})
        except Exception as exc: self.send_json(409,{"ok":False,"error":str(exc)})

def serve()->None: http.server.ThreadingHTTPServer(("0.0.0.0",PORT),Handler).serve_forever()

def main()->None:
    log(f"HA File Sync Bridge {VERSION}"); threading.Thread(target=serve,daemon=True).start()
    while True:
        result="ok"; error=""
        try:
            cfg=options()
            with LOCK.open("x"):
                if not state().get("initialized"): sync_cycle(cfg,str(cfg.get("initial_sync","ha_to_git"))); save_state(initialized=True)
                else: sync_cycle(cfg)
        except FileExistsError: result="locked"; error="another sync is running"
        except Exception as exc: result="error"; error=str(exc); log(f"REQUEST FAILED: {exc}")
        finally:
            try: LOCK.unlink()
            except FileNotFoundError: pass
            cfg=options(); atomic_json(STATUS,{"version":VERSION,"timestamp":utc_now(),"result":result,"error":error,"repo":cfg.get("config_repo"),"branch":cfg.get("branch"),"config_hash":tree_hash(CONFIG,cfg) if CONFIG.is_dir() else None,"last_sync_commit":state().get("last_sync_commit")})
        time.sleep(max(5,int(options().get("poll_interval",60))))

if __name__=="__main__": main()
