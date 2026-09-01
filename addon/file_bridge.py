#!/usr/bin/env python3
"""HA File Sync Bridge 0.5.0: structured Git file synchronization only."""
from __future__ import annotations
import json, shutil, subprocess, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
DATA=Path('/data'); CONFIG=Path('/config'); WORK=DATA/'bridge-work'; OPTIONS=DATA/'options.json'
def now(): return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def log(m): print(f'[file-bridge] {m}',flush=True)
def git(args,cwd=None):
    p=subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True,text=True,timeout=180)
    if p.stdout.strip(): log(f'git stdout: {p.stdout.strip()}')
    if p.stderr.strip(): log(f'git stderr: {p.stderr.strip()}')
    return p
def git_diagnostic(args,cwd=None):
    log(f'git command: git {" ".join(args)}')
    p=subprocess.run(['git',*args],cwd=cwd,text=True,capture_output=True,timeout=180)
    if p.stdout.strip(): log(f'git stdout: {p.stdout.strip()}')
    if p.stderr.strip(): log(f'git stderr: {p.stderr.strip()}')
    if p.returncode: log(f'git exit code: {p.returncode}'); raise subprocess.CalledProcessError(p.returncode,['git',*args],p.stdout,p.stderr)
    return p
def load_options():
    d={'poll_interval':60,'config_repo':'git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git'}
    if OPTIONS.is_file():
        x=json.loads(OPTIONS.read_text())
        if isinstance(x,dict): d.update(x)
    if not d.get('config_repo'): raise ValueError('config_repo is required')
    return d
def ensure_repo(o):
    if not (WORK/'.git').is_dir(): git_diagnostic(['clone','--no-checkout',o['config_repo'],str(WORK)])
    else:
        git_diagnostic(['remote','set-url','origin',o['config_repo']],WORK); git_diagnostic(['fetch','--prune','origin'],WORK)
def main():
    WORK.mkdir(parents=True,exist_ok=True)
    while True:
        try: ensure_repo(load_options()); log('repository access OK')
        except Exception as e: log(f'request failed: {e}')
        time.sleep(int(load_options()['poll_interval']))
if __name__=='__main__': main()
