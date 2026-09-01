#!/usr/bin/env python3
"""HA File Sync Bridge 0.5.0: structured Git file synchronization only."""
from __future__ import annotations
import json, os, shutil, subprocess, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
DATA=Path('/data'); CONFIG=Path('/config'); WORK=DATA/'bridge-work'; BACKUPS=DATA/'backups'; LAST_REQUEST=DATA/'last_request_id'; LAST_GOOD=DATA/'last_known_good.json'; OPTIONS=DATA/'options.json'
REQUEST_PATH='bridge/request.json'; STATUS_PATH='bridge/status.json'; META_PATH='bridge/snapshot.json'; CHECK_URL='http://supervisor/core/check'
ALLOWED_EXACT=frozenset({'configuration.yaml','automations.yaml','scripts.yaml','scenes.yaml','go2rtc.yaml'}); ALLOWED_PREFIXES=('packages/','dashboards/','themes/','www/'); ALLOWED_COMPONENTS=frozenset(); ALLOWED_SCOPES={'core','automations','scripts','scenes','packages','dashboards','themes','www','go2rtc','custom_components'}; DENIED_EXACT=frozenset({'secrets.yaml','home-assistant_v2.db','home-assistant_v2.db-wal','home-assistant_v2.db-shm'}); DENIED_PREFIXES=('.storage/','.cloud/')
def now(): return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def log(x): print('[file-bridge]',x,flush=True)
def git(args,cwd=None): return subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True,text=True,timeout=180)
def options():
 d={'poll_interval':60,'config_repo':'','control_branch':'bridge-control','snapshot_branch':'live-snapshot','status_branch':'bridge-status','main_branch':'main'}
 if OPTIONS.is_file():
  p=json.loads(OPTIONS.read_text());
  if isinstance(p,dict): d.update(p)
 if not isinstance(d['config_repo'],str) or not d['config_repo']: raise ValueError('config_repo is required')
 return d
def safe_rel(p):
 if not isinstance(p,str): raise ValueError('unsafe path')
 v=PurePosixPath(p)
 if not p or v.is_absolute() or '..' in v.parts or str(v)=='.': raise ValueError(f'unsafe path: {p!r}')
 return v
def allowed_path(p):
 n=str(safe_rel(p))
 if n in DENIED_EXACT or any(n==x[:-1] or n.startswith(x) for x in DENIED_PREFIXES): return False
 if n in ALLOWED_EXACT or n.startswith(ALLOWED_PREFIXES): return True
 q=PurePosixPath(n).parts
 return len(q)>2 and q[0]=='custom_components' and q[1] in ALLOWED_COMPONENTS
def scope_for(p):
 f=safe_rel(p).parts[0]; return {'configuration.yaml':'core','automations.yaml':'automations','scripts.yaml':'scripts','scenes.yaml':'scenes','go2rtc.yaml':'go2rtc','www':'www'}.get(f,f)
def files(root,scopes=None):
 out=set()
 if not root.exists(): return out
 for f in root.rglob('*'):
  if f.is_file():
   r=f.relative_to(root).as_posix()
   if allowed_path(r) and (scopes is None or scope_for(r) in scopes): out.add(r)
 return out
def ensure_repo(o):
 if not WORK.exists(): git(['clone','--no-checkout',o['config_repo'],str(WORK)])
 git(['remote','set-url','origin',o['config_repo']],cwd=WORK); git(['fetch','--prune','origin'],cwd=WORK)
def checkout(branch):
 ok=subprocess.run(['git','show-ref','--verify',f'refs/remotes/origin/{branch}'],cwd=WORK,capture_output=True).returncode==0
 if ok: git(['checkout','-B',branch,f'origin/{branch}'],cwd=WORK)
 else:
  git(['checkout','--orphan',branch],cwd=WORK)
  for p in list(WORK.iterdir()):
   if p.name!='.git': shutil.rmtree(p) if p.is_dir() else p.unlink()
def copy_allowed(src,dst,scopes=None):
 out=[]
 for r in sorted(files(src,scopes)):
  t=dst/r; t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src/r,t); out.append(r)
 return out
def write_branch(o,branch,path,payload,msg):
 checkout(branch); t=WORK/safe_rel(path); t.parent.mkdir(parents=True,exist_ok=True); t.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n'); git(['add','--',path],cwd=WORK)
 if subprocess.run(['git','diff','--cached','--quiet'],cwd=WORK).returncode!=0: git(['-c','user.name=ha-file-sync-bridge','-c','user.email=bridge@local','commit','-m',msg],cwd=WORK); git(['push','origin',f'HEAD:{branch}'],cwd=WORK)
def snapshot(o):
 checkout(o['snapshot_branch'])
 for p in list(WORK.iterdir()):
  if p.name!='.git': shutil.rmtree(p) if p.is_dir() else p.unlink()
 copied=copy_allowed(CONFIG,WORK); m={'action':'snapshot','source':'home-assistant-config','files':copied,'core_version':(CONFIG/'.HA_VERSION').read_text().strip() if (CONFIG/'.HA_VERSION').is_file() else None,'timestamp':now()}; x=WORK/META_PATH; x.parent.mkdir(parents=True,exist_ok=True); x.write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n'); git(['add','--all'],cwd=WORK)
 if subprocess.run(['git','diff','--cached','--quiet'],cwd=WORK).returncode!=0: git(['-c','user.name=ha-file-sync-bridge','-c','user.email=bridge@local','commit','-m','live snapshot'],cwd=WORK); git(['push','origin',f'HEAD:{o["snapshot_branch"]}'],cwd=WORK)
 return m
def validate_request(r,o):
 a=r.get('action');
 if a not in {'snapshot','validate','deploy','rollback','status'}: raise ValueError('unsupported action')
 s=r.get('scope',[])
 if not isinstance(s,list) or any(not isinstance(x,str) for x in s): raise ValueError('scope must be a list of strings')
 s=set(s)
 if s and not s<=ALLOWED_SCOPES: raise ValueError('invalid deploy scope')
 if a in {'validate','deploy'}:
  sha=r.get('target_commit')
  if not isinstance(sha,str) or len(sha)!=40 or any(c not in '0123456789abcdef' for c in sha.lower()): raise ValueError('target_commit must be a full SHA')
  git(['merge-base','--is-ancestor',sha,f'origin/{o["main_branch"]}'],cwd=WORK)
 return a,s or set(ALLOWED_SCOPES)
def materialize(sha):
 t=DATA/'candidate'; shutil.rmtree(t,ignore_errors=True); git(['worktree','add','--detach',str(t),sha],cwd=WORK); return t
def validate_candidate(c,s):
 fs=sorted(files(c,s))
 if not fs: raise ValueError('no allowed files in requested scope')
 for r in fs:
  if r.endswith(('.yaml','.yml')): subprocess.run(['python3','-c',"import yaml,sys; yaml.safe_load(open(sys.argv[1],encoding='utf-8'))",str(c/r)],check=True,capture_output=True,text=True,timeout=30)
 return fs
def backup(fs,s):
 b=BACKUPS/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'); b.mkdir(parents=True); existing=[]
 for r in sorted(fs):
  q=CONFIG/r
  if q.is_file(): t=b/r; t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(q,t); existing.append(r)
 (b/'manifest.json').write_text(json.dumps({'files':sorted(fs),'existing':existing,'scopes':sorted(s),'timestamp':now()},indent=2)+'\n'); return b
def restore(b,fs):
 for r in sorted(fs):
  if not allowed_path(r): raise ValueError(f'invalid backup path: {r!r}')
  s=b/r; t=CONFIG/r
  if s.is_file(): t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,t)
  elif t.exists(): t.unlink()
def check():
 tok=os.environ.get('SUPERVISOR_TOKEN')
 if not tok: raise RuntimeError('SUPERVISOR_TOKEN is missing')
 req=urllib.request.Request(CHECK_URL,method='POST',headers={'Authorization':f'Bearer {tok}','Content-Type':'application/json'},data=b'{}')
 try:
  with urllib.request.urlopen(req,timeout=180) as r: body=json.loads(r.read().decode())
 except (urllib.error.URLError,TimeoutError,json.JSONDecodeError) as e: raise RuntimeError(f'Supervisor core check failed: {e}') from e
 if not isinstance(body,dict) or body.get('result')!='ok': raise RuntimeError(f'Supervisor core check failed: {body!r}')
def deploy(sha,s):
 c=materialize(sha)
 try:
  cand=set(validate_candidate(c,s)); cur=files(CONFIG,s); affected=cur|cand; b=backup(affected,s)
  removed=[]
  src=files(c,s)
  for r in sorted(cur-src): (CONFIG/r).unlink(); removed.append(r)
  for r in sorted(cand): t=CONFIG/r; t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(c/r,t)
  try: check()
  except Exception: restore(b,affected); raise
  LAST_GOOD.write_text(json.dumps({'commit':sha,'backup':str(b),'files':sorted(affected),'scopes':sorted(s)},indent=2)+'\n'); return {'action':'deploy','ok':True,'commit':sha,'files':sorted(cand),'removed':removed}
 finally: subprocess.run(['git','worktree','remove','--force',str(c)],cwd=WORK,capture_output=True,timeout=60)
def rollback():
 if not LAST_GOOD.is_file(): raise ValueError('no last-known-good deployment is available')
 p=json.loads(LAST_GOOD.read_text()); b=Path(p['backup']); fs=set(p.get('files',[]))
 if not b.is_dir() or not isinstance(p.get('files',[]),list): raise ValueError('last-known-good backup is invalid')
 cur=files(CONFIG); safety=backup(cur|fs,ALLOWED_SCOPES)
 try:
  restore(b,fs); check()
 except Exception:
  restore(safety,cur|fs); raise RuntimeError('rollback validation failed; pre-rollback state restored')
 return {'action':'rollback','ok':True,'commit':p['commit'],'files':sorted(fs)}
def process(o):
 ensure_repo(o); r=subprocess.run(['git','show',f'origin/{o["control_branch"]}:{REQUEST_PATH}'],cwd=WORK,capture_output=True,text=True,timeout=60)
 if r.returncode: return
 try: req=json.loads(r.stdout)
 except json.JSONDecodeError: return
 if not isinstance(req,dict): return
 rid=req.get('id')
 if not isinstance(rid,str) or not rid.strip(): raise ValueError('request id is required')
 if LAST_REQUEST.exists() and LAST_REQUEST.read_text().strip()==rid: return
 try:
  a,s=validate_request(req,o)
  if a=='snapshot': result=snapshot(o)
  elif a=='deploy': result=deploy(req['target_commit'],s)
  elif a=='rollback': result=rollback()
  elif a=='status': result={'action':'status','ok':True,'last_known_good':json.loads(LAST_GOOD.read_text()) if LAST_GOOD.exists() else None}
  else:
   c=materialize(req['target_commit'])
   try: result={'action':'validate','ok':True,'files':validate_candidate(c,s)}
   finally: subprocess.run(['git','worktree','remove','--force',str(c)],cwd=WORK,capture_output=True,timeout=60)
 except Exception as e: result={'action':a if 'a' in locals() else str(req.get('action','unknown')),'ok':False,'error':str(e)}
 result['request_id']=rid
 write_branch(o,o['status_branch'],STATUS_PATH,result,f'bridge status: {result["action"]}'); LAST_REQUEST.write_text(rid+'\n')
def main():
 while True:
  try: process(options())
  except Exception as e: log(f'request failed: {e}')
  time.sleep(int(options()['poll_interval']))
if __name__=='__main__': main()
