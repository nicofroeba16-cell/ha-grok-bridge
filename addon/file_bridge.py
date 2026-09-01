#!/usr/bin/env python3
"""HA File Sync Bridge 0.5.0: structured Git file synchronization only."""
from __future__ import annotations
import json, shutil, subprocess, time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

DATA=Path("/data"); CONFIG=Path("/config"); WORK=DATA/"bridge-work"; BACKUPS=DATA/"backups"
LAST_REQUEST=DATA/"last_request_id"; LAST_GOOD=DATA/"last_known_good.json"; OPTIONS=DATA/"options.json"
REQUEST_PATH="bridge/request.json"; STATUS_PATH="bridge/status.json"; META_PATH="bridge/snapshot.json"
ALLOWED_EXACT=frozenset({"configuration.yaml","automations.yaml","scripts.yaml","scenes.yaml","go2rtc.yaml"})
ALLOWED_PREFIXES=("packages/","dashboards/","themes/","www/")
ALLOWED_COMPONENTS=frozenset()
ALLOWED_SCOPES={"core","automations","scripts","scenes","packages","dashboards","themes","www","go2rtc","custom_components"}
DENIED_EXACT=frozenset({"secrets.yaml","home-assistant_v2.db","home-assistant_v2.db-wal","home-assistant_v2.db-shm"})
DENIED_PREFIXES=(".storage/",".cloud/")

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def log(m): print(f"[file-bridge] {m}",flush=True)
def git(args,cwd=None): return subprocess.run(["git",*args],cwd=cwd,check=True,capture_output=True,text=True,timeout=180)

def load_options():
    d={"poll_interval":60,"config_repo":"","control_branch":"bridge-control","snapshot_branch":"live-snapshot","status_branch":"bridge-status","main_branch":"main"}
    if OPTIONS.is_file():
        p=json.loads(OPTIONS.read_text()); d.update(p if isinstance(p,dict) else {})
    if not isinstance(d["config_repo"],str) or not d["config_repo"]: raise ValueError("config_repo is required")
    return d

def safe_rel(path):
    v=PurePosixPath(path)
    if not isinstance(path,str) or not path or v.is_absolute() or ".." in v.parts or str(v)==".": raise ValueError(f"unsafe path: {path!r}")
    return v

def allowed_path(path):
    n=str(safe_rel(path))
    if n in DENIED_EXACT or any(n==p[:-1] or n.startswith(p) for p in DENIED_PREFIXES): return False
    if n in ALLOWED_EXACT or n.startswith(ALLOWED_PREFIXES): return True
    p=PurePosixPath(n).parts
    return len(p)>2 and p[0]=="custom_components" and p[1] in ALLOWED_COMPONENTS

def scope_for(path):
    first=safe_rel(path).parts[0]
    return {"configuration.yaml":"core","automations.yaml":"automations","scripts.yaml":"scripts","scenes.yaml":"scenes","go2rtc.yaml":"go2rtc","www":"www"}.get(first,first)

def allowed_files(root,scopes=None):
    out=set()
    for f in root.rglob("*"):
        if f.is_file():
            r=f.relative_to(root).as_posix()
            if allowed_path(r) and (scopes is None or scope_for(r) in scopes): out.add(r)
    return out

def copy_files(src,dst,scopes=None):
    done=[]
    for r in sorted(allowed_files(src,scopes)):
        t=dst/r; t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src/r,t); done.append(r)
    return done

def ensure_repo(o):
    if not WORK.exists(): git(["clone","--no-checkout",o["config_repo"],str(WORK)])
    git(["remote","set-url","origin",o["config_repo"]],cwd=WORK); git(["fetch","--prune","origin"],cwd=WORK)

def checkout_branch(branch):
    exists=subprocess.run(["git","show-ref","--verify",f"refs/remotes/origin/{branch}"],cwd=WORK,capture_output=True).returncode==0
    if exists: git(["checkout","-B",branch,f"origin/{branch}"],cwd=WORK)
    else:
        git(["checkout","--orphan",branch],cwd=WORK)
        for p in list(WORK.iterdir()):
            if p.name!=".git": shutil.rmtree(p) if p.is_dir() else p.unlink()

def push_branch(branch): git(["push","origin",f"HEAD:{branch}"],cwd=WORK)

def write_json_branch(o,branch,path,payload,message):
    checkout_branch(branch); t=WORK/safe_rel(path); t.parent.mkdir(parents=True,exist_ok=True); t.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n")
    git(["add","--all"],cwd=WORK)
    if subprocess.run(["git","diff","--cached","--quiet"],cwd=WORK).returncode!=0:
        git(["-c","user.name=ha-file-sync-bridge","-c","user.email=bridge@local","commit","-m",message],cwd=WORK); push_branch(branch)

def read_request(o):
    r=subprocess.run(["git","show",f"origin/{o['control_branch']}:{REQUEST_PATH}"],cwd=WORK,capture_output=True,text=True,timeout=60)
    if r.returncode!=0:return None
    try:p=json.loads(r.stdout)
    except json.JSONDecodeError:return None
    return p if isinstance(p,dict) else None

def validate_request(req,o):
    a=req.get("action")
    if a not in {"snapshot","validate","deploy","rollback","status"}: raise ValueError("unsupported action")
    raw=req.get("scope",[])
    if not isinstance(raw,list) or any(not isinstance(x,str) for x in raw): raise ValueError("scope must be a list of strings")
    scopes=set(raw)
    if scopes and not scopes<=ALLOWED_SCOPES: raise ValueError("invalid deploy scope")
    if a in {"validate","deploy"}:
        sha=req.get("target_commit")
        if not isinstance(sha,str) or len(sha)!=40 or any(c not in "0123456789abcdef" for c in sha.lower()): raise ValueError("target_commit must be a full SHA")
        git(["merge-base","--is-ancestor",sha,f"origin/{o['main_branch']}"],cwd=WORK)
    return a,scopes or set(ALLOWED_SCOPES)

def snapshot(o):
    checkout_branch(o["snapshot_branch"])
    for p in list(WORK.iterdir()):
        if p.name!=".git": shutil.rmtree(p) if p.is_dir() else p.unlink()
    files=copy_files(CONFIG,WORK)
    meta={"action":"snapshot","source":"home-assistant-config","files":files,"core_version":(CONFIG/".HA_VERSION").read_text().strip() if (CONFIG/".HA_VERSION").is_file() else None,"timestamp":now()}
    m=WORK/META_PATH; m.parent.mkdir(parents=True,exist_ok=True); m.write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n")
    git(["add","--all"],cwd=WORK)
    if subprocess.run(["git","diff","--cached","--quiet"],cwd=WORK).returncode!=0:
        git(["-c","user.name=ha-file-sync-bridge","-c","user.email=bridge@local","commit","-m","live snapshot"],cwd=WORK); push_branch(o["snapshot_branch"])
    return meta

def materialize(sha):
    c=DATA/"candidate"; shutil.rmtree(c,ignore_errors=True); git(["worktree","add","--detach",str(c),sha],cwd=WORK); return c

def validate_candidate(c,scopes):
    files=sorted(allowed_files(c,scopes))
    if not files: raise ValueError("no allowed files in requested scope")
    for r in files:
        if r.endswith((".yaml",".yml")):
            subprocess.run(["python3","-c","import yaml,sys; yaml.safe_load(open(sys.argv[1],encoding='utf-8'))",str(c/r)],check=True,capture_output=True,text=True,timeout=30)
    return files

def make_backup(files,scopes):
    b=BACKUPS/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); b.mkdir(parents=True)
    existing=set()
    for r in sorted(files):
        s=CONFIG/r
        if s.is_file(): t=b/r; t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,t); existing.add(r)
    (b/"manifest.json").write_text(json.dumps({"files":sorted(files),"existing":sorted(existing),"scopes":sorted(scopes),"timestamp":now()},indent=2)+"\n")
    return b

def restore(b,files):
    for r in sorted(files):
        s=b/r; t=CONFIG/r
        if s.is_file(): t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,t)
        elif t.exists(): t.unlink()

def deploy(sha,scopes):
    c=materialize(sha)
    try:
        candidate=set(validate_candidate(c,scopes)); current=allowed_files(CONFIG,scopes); affected=candidate|current; b=make_backup(affected,scopes)
        for r in sorted(current-candidate): (CONFIG/r).unlink()
        for r in sorted(candidate): t=CONFIG/r; t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(c/r,t)
        check=subprocess.run(["ha","core","check"],capture_output=True,text=True,timeout=180)
        if check.returncode:
            restore(b,affected); raise RuntimeError(f"ha core check failed: {(check.stdout+check.stderr)[-1000:]}")
        LAST_GOOD.write_text(json.dumps({"commit":sha,"backup":str(b),"files":sorted(affected)})+"\n")
        return {"action":"deploy","ok":True,"commit":sha,"files":sorted(candidate),"removed":sorted(current-candidate)}
    finally: subprocess.run(["git","worktree","remove","--force",str(c)],cwd=WORK,capture_output=True,timeout=60)

def rollback():
    if not LAST_GOOD.is_file(): raise ValueError("no last-known-good deployment is available")
    p=json.loads(LAST_GOOD.read_text()); b=Path(p["backup"]); files=p.get("files",[])
    if not b.is_dir() or not isinstance(files,list): raise ValueError("last-known-good backup is invalid")
    for r in files:
        if not isinstance(r,str) or not allowed_path(r): raise ValueError("backup contains an invalid path")
    restore(b,files); check=subprocess.run(["ha","core","check"],capture_output=True,text=True,timeout=180)
    if check.returncode: raise RuntimeError(f"rollback validation failed: {(check.stdout+check.stderr)[-1000:]}")
    return {"action":"rollback","ok":True,"commit":p["commit"],"files":files}

def process(o):
    ensure_repo(o); req=read_request(o); last=LAST_REQUEST.read_text().strip() if LAST_REQUEST.exists() else ""
    if not req or req.get("id")==last:return
    a,scopes=validate_request(req,o)
    if a=="snapshot": result=snapshot(o)
    elif a=="deploy": result=deploy(req["target_commit"],scopes)
    elif a=="rollback": result=rollback()
    elif a=="validate":
        c=materialize(req["target_commit"])
        try: result={"action":"validate","ok":True,"files":validate_candidate(c,scopes)}
        finally: subprocess.run(["git","worktree","remove","--force",str(c)],cwd=WORK,capture_output=True,timeout=60)
    else: result={"action":"status","ok":True,"last_known_good":json.loads(LAST_GOOD.read_text()) if LAST_GOOD.exists() else None}
    result["request_id"]=req.get("id"); write_json_branch(o,o["status_branch"],STATUS_PATH,{**result,"timestamp":now()},f"bridge status: {a}"); LAST_REQUEST.write_text(str(req.get("id"))+"\n")

def main():
    while True:
        try: process(load_options())
        except Exception as e: log(f"request failed: {e}")
        time.sleep(int(load_options()["poll_interval"]))

if __name__=="__main__": main()
