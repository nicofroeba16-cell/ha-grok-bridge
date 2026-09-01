#!/usr/bin/env python3
from __future__ import annotations
import json, shutil, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
VERSION = "1.0.1"
DATA=Path('/data'); CONFIG=Path('/config'); WORK=DATA/'bridge-work'; SNAPSHOTS=DATA/'snapshots'; OPTIONS=DATA/'options.json'
EXCLUDED_NAMES={'.git','.storage','.cloud','.HA_VERSION','.ssh','.cache','secrets.yaml','home-assistant_v2.db','home-assistant_v2.db-shm','home-assistant_v2.db-wal','home-assistant_v2.db-journal','home-assistant.log','home-assistant.log.1','home-assistant.log.fault'}
EXCLUDED_DIRS={'tts','media','backups'}
EXCLUDED_SUFFIXES={'.passphrase','.pem','.key','.p12','.pfx'}
def log(m:str)->None: print(f'[file-bridge] {m}',flush=True)
def run_git(args:list[str],cwd:Path=WORK,check:bool=True)->subprocess.CompletedProcess[str]:
 log(f'git command: git {" ".join(args)}'); p=subprocess.run(['git',*args],cwd=cwd,check=False,capture_output=True,text=True,timeout=180)
 if p.stdout.strip(): log(f'git stdout: {p.stdout.strip()}')
 if p.stderr.strip(): log(f'git stderr: {p.stderr.strip()}')
 if check and p.returncode: raise RuntimeError(f'git exited with {p.returncode}: {p.stderr.strip() or p.stdout.strip()}')
 return p
def load_options()->dict[str,Any]:
 d={'poll_interval':60,'config_repo':'git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git','branch':'main','sync_config_to_git':True}
 if OPTIONS.is_file():
  try:
   v=json.loads(OPTIONS.read_text(encoding='utf-8')); d.update(v) if isinstance(v,dict) else None
  except Exception as e: log(f'options warning: {e}')
 return d
def excluded(name:str)->bool:
 return name in EXCLUDED_NAMES or name in EXCLUDED_DIRS or any(name.endswith(s) for s in EXCLUDED_SUFFIXES)
def ignore_config(directory:str,names:list[str])->set[str]: return {n for n in names if excluded(n)}
def ensure_repo(url:str,branch:str)->None:
 DATA.mkdir(parents=True,exist_ok=True)
 if not (WORK/'.git').is_dir():
  if WORK.exists(): shutil.rmtree(WORK)
  run_git(['clone','--no-checkout',url,str(WORK)],cwd=DATA); run_git(['checkout','-B',branch,f'origin/{branch}'])
 else:
  run_git(['remote','set-url','origin',url]); run_git(['fetch','--prune','origin']); run_git(['checkout',branch]); run_git(['reset','--hard',f'origin/{branch}'])
def validate()->bool:
 p=run_git(['fsck','--no-progress'],check=False); log('repository valid' if p.returncode==0 else 'repository invalid'); return p.returncode==0
def snapshot()->None:
 if CONFIG.is_dir():
  SNAPSHOTS.mkdir(parents=True,exist_ok=True); target=SNAPSHOTS/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); shutil.copytree(CONFIG,target,ignore=ignore_config)
def sync_config()->int:
 if not CONFIG.is_dir(): raise RuntimeError('/config is not mapped')
 copied=0
 for source in CONFIG.iterdir():
  if excluded(source.name): continue
  dest=WORK/source.name
  if source.is_dir():
   if dest.exists(): shutil.rmtree(dest)
   shutil.copytree(source,dest,ignore=ignore_config)
  else: shutil.copy2(source,dest)
  copied+=1
 return copied
def sync_to_github(branch:str)->None:
 n=sync_config(); log(f'/config sync: {n} top-level items prepared'); run_git(['add','-A']); changes=run_git(['status','--short'],check=False).stdout.strip()
 if not changes: log('/config unchanged; nothing to commit'); return
 run_git(['config','user.name','HA File Sync Bridge']); run_git(['config','user.email','ha-file-sync-bridge@localhost']); ts=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'); run_git(['commit','-m',f'Sync Home Assistant /config - {ts}']); run_git(['push','origin',branch]); log('HA /config synchronized to GitHub')
def main()->None:
 log(f'HA File Sync Bridge {VERSION}')
 while True:
  try:
   c=load_options(); b=str(c.get('branch','main')); ensure_repo(str(c['config_repo']),b)
   if not validate(): raise RuntimeError('repository invalid')
   log('repository access OK')
   if bool(c.get('sync_config_to_git',True)): snapshot(); sync_to_github(b)
  except Exception as e: log(f'REQUEST FAILED: {e}')
  time.sleep(max(5,int(load_options().get('poll_interval',60))))
if __name__=='__main__': main()
