#!/usr/bin/env python3
from __future__ import annotations
import hashlib,http.server,json,re,shutil,subprocess,threading,time
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
VERSION="1.0.0"; DATA=Path('/data'); CONFIG=Path('/config'); WORK=DATA/'bridge-work'; SNAPSHOTS=DATA/'snapshots'; OPTIONS=DATA/'options.json'; STATE=DATA/'state.json'; STATUS=DATA/'status.json'; LOCK=DATA/'sync.lock'; PORT=8099
DEFAULT_EXCLUDED_NAMES={'.git','.storage','.cloud','.HA_VERSION','.ssh','.cache','secrets.yaml','home-assistant_v2.db','home-assistant_v2.db-shm','home-assistant_v2.db-wal','home-assistant_v2.db-journal','home-assistant.log','home-assistant.log.1','home-assistant.log.fault'}
DEFAULT_EXCLUDED_DIRS={'tts','media','backups'}; DEFAULT_EXCLUDED_SUFFIXES={'.passphrase','.pem','.key','.p12','.pfx'}
SECRET_PATTERNS=[re.compile(r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----'),re.compile(r'(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]'),re.compile(r'(?i)\b(password|passwd|token|secret)\s*[:=]\s*[\'\"][^\'\"]+[\'\"]')]
def utc_now(): return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
def log(m): print(f'[file-bridge] {m}',flush=True)
def atomic_json(p,v):
 t=p.with_suffix(p.suffix+'.tmp'); t.write_text(json.dumps(v,indent=2,sort_keys=True),encoding='utf-8'); t.replace(p)
def load_json(p,d):
 try:
  v=json.loads(p.read_text(encoding='utf-8')) if p.is_file() else d; return v if isinstance(v,dict) else d
 except Exception: return d
def options():
 d={'poll_interval':60,'config_repo':'git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git','branch':'main','sync_mode':'bidirectional','initial_sync':'ha_to_git','dry_run':False,'max_snapshots':10,'exclude_names':','.join(sorted(DEFAULT_EXCLUDED_NAMES)),'exclude_dirs':','.join(sorted(DEFAULT_EXCLUDED_DIRS)),'exclude_suffixes':','.join(sorted(DEFAULT_EXCLUDED_SUFFIXES)),'secret_scan':True,'history_cleanup':False}; d.update(load_json(OPTIONS,{})); return d
def csv_set(v,f): return {x.strip() for x in v.split(',') if x.strip()} if isinstance(v,str) else f
def excluded(n,c): return n in csv_set(c.get('exclude_names'),DEFAULT_EXCLUDED_NAMES) or n in csv_set(c.get('exclude_dirs'),DEFAULT_EXCLUDED_DIRS) or any(n.endswith(s) for s in csv_set(c.get('exclude_suffixes'),DEFAULT_EXCLUDED_SUFFIXES))
def ignore_config(c): return lambda _d,names:{n for n in names if excluded(n,c)}
def run_git(args,cwd=WORK,check=True):
 log(f'git command: git {" ".join(args)}'); p=subprocess.run(['git',*args],cwd=cwd,check=False,capture_output=True,text=True,timeout=180)
 if p.stdout.strip(): log(f'git stdout: {p.stdout.strip()}')
 if p.stderr.strip(): log(f'git stderr: {p.stderr.strip()}')
 if check and p.returncode: raise RuntimeError(f'git exited with {p.returncode}: {p.stderr.strip() or p.stdout.strip()}')
 return p
def ensure_repo(url,branch):
 DATA.mkdir(parents=True,exist_ok=True)
 if not (WORK/'.git').is_dir():
  if WORK.exists(): shutil.rmtree(WORK)
  run_git(['clone','--no-checkout',url,str(WORK)],cwd=DATA); run_git(['checkout','-B',branch,f'origin/{branch}'])
 else:
  run_git(['remote','set-url','origin',url]); run_git(['fetch','--prune','origin']); run_git(['checkout',branch])
def validate_repo():
 if run_git(['fsck','--no-progress'],check=False).returncode: raise RuntimeError('repository invalid')
 log('repository valid'); log('repository access OK')
def tree_hash(root,c):
 h=hashlib.sha256()
 if not root.is_dir(): return h.hexdigest()
 for p in sorted(root.rglob('*')):
  rel=p.relative_to(root).as_posix()
  if any(excluded(x,c) for x in Path(rel).parts): continue
  if p.is_file(): h.update(rel.encode()); h.update(b'\0'); h.update(p.read_bytes())
 return h.hexdigest()
def snapshot(c):
 if not CONFIG.is_dir(): raise RuntimeError('/config is not mapped')
 SNAPSHOTS.mkdir(parents=True,exist_ok=True); target=SNAPSHOTS/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'); shutil.copytree(CONFIG,target,ignore=ignore_config(c)); limit=max(0,int(c.get('max_snapshots',10))); ss=sorted(p for p in SNAPSHOTS.iterdir() if p.is_dir())
 if limit and len(ss)>limit:
  for old in ss[:-limit]: shutil.rmtree(old,ignore_errors=True)
 return target
def clear_worktree():
 for p in WORK.iterdir():
  if p.name!='.git': shutil.rmtree(p) if p.is_dir() else p.unlink()
def copy_config_to_work(c):
 if not CONFIG.is_dir(): raise RuntimeError('/config is not mapped')
 clear_worktree(); n=0
 for s in CONFIG.iterdir():
  if excluded(s.name,c): continue
  d=WORK/s.name; shutil.copytree(s,d,ignore=ignore_config(c)) if s.is_dir() else shutil.copy2(s,d); n+=1
 return n
def copy_work_to_config(c,dry=False):
 if not CONFIG.is_dir(): raise RuntimeError('/config is not mapped')
 remote={p.name for p in WORK.iterdir() if p.name!='.git'}; n=0
 for s in WORK.iterdir():
  if s.name=='.git' or excluded(s.name,c): continue
  d=CONFIG/s.name
  if not dry:
   if d.exists(): shutil.rmtree(d) if d.is_dir() else d.unlink()
   shutil.copytree(s,d,ignore=ignore_config(c)) if s.is_dir() else shutil.copy2(s,d)
  n+=1
 for d in CONFIG.iterdir():
  if not excluded(d.name,c) and d.name not in remote and not dry: shutil.rmtree(d) if d.is_dir() else d.unlink()
 return n
def secret_scan(c):
 if not bool(c.get('secret_scan',True)): return []
 found=[]
 for p in WORK.rglob('*'):
  if not p.is_file() or '.git' in p.parts or excluded(p.name,c): continue
  try: text=p.read_text(encoding='utf-8',errors='ignore')
  except Exception: continue
  if any(x.search(text) for x in SECRET_PATTERNS): found.append(p.relative_to(WORK).as_posix())
 return found
def commit_push(branch,dry):
 run_git(['add','-A']); changes=run_git(['status','--short'],check=False).stdout.strip()
 if not changes: log('no changes'); return False
 if dry: log('DRY-RUN: commit/push skipped'); return True
 run_git(['config','user.name','HA File Sync Bridge']); run_git(['config','user.email','ha-file-sync-bridge@localhost']); run_git(['commit','-m',f'Sync Home Assistant /config - {utc_now()}']); run_git(['push','origin',branch]); return True
def state(): return load_json(STATE,{})
def save_state(**kw): s=state(); s.update(kw); atomic_json(STATE,s)
def sync_cycle(c,force=None):
 if not CONFIG.is_dir(): raise RuntimeError('/config is not mapped')
 b=str(c.get('branch','main')); dry=bool(c.get('dry_run',False)); mode=force or str(c.get('sync_mode','bidirectional')); ensure_repo(str(c['config_repo']),b); validate_repo(); last=state().get('last_sync_commit'); local=tree_hash(CONFIG,c)!=state().get('last_config_hash'); run_git(['fetch','--prune','origin']); remote_head=run_git(['rev-parse',f'origin/{b}']).stdout.strip(); remote=bool(last and remote_head!=last)
 if mode=='git_to_ha':
  if last and local and remote: raise RuntimeError('SYNC CONFLICT: /config and GitHub changed')
  run_git(['reset','--hard',f'origin/{b}']); snapshot(c); log(f'GitHub -> /config: {copy_work_to_config(c,dry)} items prepared')
 elif mode=='ha_to_git':
  snapshot(c); log(f'/config sync: {copy_config_to_work(c)} top-level items prepared'); f=secret_scan(c)
  if f: raise RuntimeError('SECRET SCAN BLOCKED: '+', '.join(f))
  commit_push(b,dry)
 else:
  if last and local and remote: raise RuntimeError('SYNC CONFLICT: both /config and GitHub changed')
  if remote and not local:
   run_git(['reset','--hard',f'origin/{b}']); snapshot(c); log(f'GitHub -> /config: {copy_work_to_config(c,dry)} items prepared')
  elif local or not last:
   snapshot(c); log(f'/config sync: {copy_config_to_work(c)} top-level items prepared'); f=secret_scan(c)
   if f: raise RuntimeError('SECRET SCAN BLOCKED: '+', '.join(f))
   commit_push(b,dry)
 save_state(last_sync_commit=run_git(['rev-parse','HEAD']).stdout.strip(),last_config_hash=tree_hash(CONFIG,c))
def restore_snapshot(name,c,dry=False):
 src=SNAPSHOTS/name
 if not src.is_dir(): raise RuntimeError('snapshot not found')
 if not dry: snapshot(c)
 for p in list(CONFIG.iterdir()):
  if not excluded(p.name,c) and not dry: shutil.rmtree(p) if p.is_dir() else p.unlink()
 if not dry:
  for p in src.iterdir():
   if excluded(p.name,c): continue
   d=CONFIG/p.name; shutil.copytree(p,d,ignore=ignore_config(c)) if p.is_dir() else shutil.copy2(p,d)
 log(f"snapshot restore {'planned' if dry else 'completed'}: {name}")
def history_cleanup(c):
 if not bool(c.get('history_cleanup',False)): raise RuntimeError('history cleanup is disabled')
 paths=csv_set(c.get('exclude_names'),DEFAULT_EXCLUDED_NAMES)|csv_set(c.get('exclude_dirs'),DEFAULT_EXCLUDED_DIRS)|{'*.passphrase','*.pem','*.key','*.p12','*.pfx'}; script='git rm -r --cached --ignore-unmatch '+' '.join(sorted(paths))
 run_git(['filter-branch','--force','--index-filter',script,'--prune-empty','--tag-name-filter','cat','--','--all']); run_git(['reflog','expire','--expire=now','--all']); run_git(['gc','--prune=now','--aggressive']); run_git(['push','--force','--all','origin']); run_git(['push','--force','--tags','origin'],check=False); log('history cleanup completed')
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*_): pass
 def send_json(self,code,v):
  raw=json.dumps(v,indent=2).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
 def do_GET(self):
  if self.path=='/':
   s=load_json(STATUS,{'version':VERSION}); snaps=sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir()) if SNAPSHOTS.exists() else []; body=f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>HA File Sync Bridge</title></head><body><h1>HA File Sync Bridge {VERSION}</h1><pre>{json.dumps(s,indent=2)}</pre><form method="post" action="/sync/up"><button>HA → GitHub</button></form><form method="post" action="/sync/down"><button>GitHub → HA</button></form><h2>Snapshots</h2><pre>{json.dumps(snaps,indent=2)}</pre></body></html>'''.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
  if self.path=='/status': self.send_json(200,load_json(STATUS,{'version':VERSION})); return
  if self.path=='/snapshots': self.send_json(200,{'snapshots':[p.name for p in sorted(SNAPSHOTS.iterdir()) if p.is_dir()] if SNAPSHOTS.exists() else []}); return
  self.send_json(404,{'error':'not found'})
 def do_POST(self):
  c=options()
  try:
   if self.path=='/sync/up': sync_cycle(c,'ha_to_git')
   elif self.path=='/sync/down': sync_cycle(c,'git_to_ha')
   elif self.path.startswith('/restore/'): restore_snapshot(self.path.split('/',2)[2],c,bool(c.get('dry_run')))
   elif self.path=='/history-cleanup': history_cleanup(c)
   else: self.send_json(404,{'error':'not found'}); return
   self.send_json(200,{'ok':True})
  except Exception as e: self.send_json(409,{'ok':False,'error':str(e)})
def serve(): http.server.ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
def main():
 log(f'HA File Sync Bridge {VERSION}'); threading.Thread(target=serve,daemon=True).start()
 while True:
  result='ok'; error=''
  try:
   c=options()
   with LOCK.open('x'):
    if not state().get('initialized'): sync_cycle(c,str(c.get('initial_sync','ha_to_git'))); save_state(initialized=True)
    else: sync_cycle(c)
  except FileExistsError: result='locked'; error='another sync is running'
  except Exception as e: result='error'; error=str(e); log(f'REQUEST FAILED: {e}')
  finally:
   try: LOCK.unlink()
   except FileNotFoundError: pass
   c=options(); atomic_json(STATUS,{'version':VERSION,'timestamp':utc_now(),'result':result,'error':error,'repo':c.get('config_repo'),'branch':c.get('branch'),'config_hash':tree_hash(CONFIG,c) if CONFIG.is_dir() else None,'last_sync_commit':state().get('last_sync_commit')})
  time.sleep(max(5,int(options().get('poll_interval',60))))
if __name__=='__main__': main()
