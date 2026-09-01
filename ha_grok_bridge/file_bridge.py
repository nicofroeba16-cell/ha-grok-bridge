#!/usr/bin/env python3
from __future__ import annotations
import hashlib,http.server,json,os,re,shutil,subprocess,threading,time
from datetime import datetime,timezone
from pathlib import Path
VERSION="1.0.3"; DATA=Path("/data"); CONFIG=Path("/config"); WORK=DATA/"bridge-work"; SNAPSHOTS=DATA/"snapshots"; OPTIONS=DATA/"options.json"; STATE=DATA/"state.json"; STATUS=DATA/"status.json"; LOCK=DATA/"sync.lock"; PORT=8099
DEFAULT_EXCLUDED_NAMES={".git",".storage",".cloud",".HA_VERSION",".ssh",".cache","secrets.yaml","home-assistant_v2.db","home-assistant_v2.db-shm","home-assistant_v2.db-wal","home-assistant_v2.db-journal","home-assistant.log","home-assistant.log.1","home-assistant.log.fault"}
DEFAULT_EXCLUDED_DIRS={"tts","media","backups"}; DEFAULT_EXCLUDED_SUFFIXES={".passphrase",".pem",".key",".p12",".pfx"}
SENSITIVE_NAMES={"secrets.yaml",".env",".env.local",".env.production",".env.development","credentials.json","credentials.yaml","token.json","service-account.json","ha-grok-bridge.passphrase","ha-file-sync-bridge.passphrase"}; SENSITIVE_SUFFIXES=DEFAULT_EXCLUDED_SUFFIXES
SECRET_PATTERNS=[re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----"),re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),re.compile(r"\bAKIA[0-9A-Z]{16}\b"),re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")]
CONFIG_SECRET_RE=re.compile(r'''(?im)^\s*(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|secret|token)\s*[:=]\s*["']([^"']{12,})["']\s*(?:#.*)?$''')
SCANNABLE={".yaml",".yml",".json",".env",".ini",".conf",".cfg",".toml",".txt",".sh"}

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
def log(s): print(f"[file-bridge] {s}",flush=True)
def load(p,d):
 try:
  v=json.loads(p.read_text(encoding="utf-8")) if p.is_file() else d
  return v if isinstance(v,dict) else d
 except Exception:return d
def save(p,v):
 p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix(p.suffix+".tmp"); t.write_text(json.dumps(v,indent=2,sort_keys=True),encoding="utf-8"); t.replace(p)
def cfg():
 d={"poll_interval":60,"config_repo":"git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git","branch":"main","sync_mode":"bidirectional","initial_sync":"ha_to_git","dry_run":False,"max_snapshots":10,"exclude_names":",".join(sorted(DEFAULT_EXCLUDED_NAMES)),"exclude_dirs":",".join(sorted(DEFAULT_EXCLUDED_DIRS)),"exclude_suffixes":",".join(sorted(DEFAULT_EXCLUDED_SUFFIXES)),"secret_scan":True,"history_cleanup":False}; d.update(load(OPTIONS,{})); return d
def csv(v,f): return {x.strip() for x in v.split(",") if x.strip()} if isinstance(v,str) else set(f)
def excluded(n,c): return n in csv(c.get("exclude_names"),DEFAULT_EXCLUDED_NAMES) or n in csv(c.get("exclude_dirs"),DEFAULT_EXCLUDED_DIRS) or any(n.endswith(x) for x in csv(c.get("exclude_suffixes"),DEFAULT_EXCLUDED_SUFFIXES))
def ignored(p,c): return any(excluded(x,c) for x in p.parts)
def ignore(c): return lambda _d,n:{x for x in n if excluded(x,c)}
def git(a,cwd=WORK,check=True):
 p=subprocess.run(["git",*a],cwd=cwd,capture_output=True,text=True,timeout=180)
 if check and p.returncode: raise RuntimeError(f"git failed ({p.returncode}): {(p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else 'unknown'}")
 return p
def repo(url,b):
 DATA.mkdir(parents=True,exist_ok=True)
 if not (WORK/".git").is_dir():
  if WORK.exists(): shutil.rmtree(WORK)
  git(["clone","--no-checkout",url,str(WORK)],DATA); git(["checkout","-B",b,f"origin/{b}"])
 else: git(["remote","set-url","origin",url]); git(["fetch","--prune","origin"]); git(["checkout",b])
def treehash(root,c):
 h=hashlib.sha256()
 if root.is_dir():
  for p in sorted(root.rglob("*")):
   r=p.relative_to(root)
   if p.is_file() and not ignored(r,c): h.update(r.as_posix().encode()+b"\0"+p.read_bytes())
 return h.hexdigest()
def snapshot(c):
 SNAPSHOTS.mkdir(parents=True,exist_ok=True); t=SNAPSHOTS/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"); shutil.copytree(CONFIG,t,ignore=ignore(c)); lim=max(0,int(c.get("max_snapshots",10))); xs=sorted(p for p in SNAPSHOTS.iterdir() if p.is_dir());
 if lim and len(xs)>lim:
  for p in xs[:-lim]: shutil.rmtree(p,ignore_errors=True)
 return t
def clear(c):
 for p in list(WORK.iterdir()):
  if p.name==".git" or excluded(p.name,c): continue
  shutil.rmtree(p) if p.is_dir() else p.unlink()
def to_work(c):
 clear(c); n=0
 for s in CONFIG.iterdir():
  if excluded(s.name,c): continue
  d=WORK/s.name; shutil.copytree(s,d,ignore=ignore(c)) if s.is_dir() else shutil.copy2(s,d); n+=1
 return n
def tracked_sensitive():
 out=[]
 for x in git(["ls-files"]).stdout.splitlines():
  p=Path(x)
  if any(q in SENSITIVE_NAMES or q.endswith(tuple(SENSITIVE_SUFFIXES)) for q in p.parts): out.append(x)
 return out
def secret_scan(c):
 if not bool(c.get("secret_scan",True)): return []
 out=[]
 for p in WORK.rglob("*"):
  if not p.is_file() or ".git" in p.parts: continue
  r=p.relative_to(WORK)
  if ignored(r,c): continue
  if p.name in SENSITIVE_NAMES or any(q.endswith(tuple(SENSITIVE_SUFFIXES)) for q in r.parts): out.append(r.as_posix()); continue
  if p.suffix.lower() not in SCANNABLE: continue
  try:t=p.read_text(encoding="utf-8",errors="ignore")
  except Exception:continue
  if any(x.search(t) for x in SECRET_PATTERNS): out.append(r.as_posix()); continue
  if p.suffix.lower() in {".yaml",".yml",".json",".env",".ini",".conf",".cfg",".toml"}:
   for m in CONFIG_SECRET_RE.finditer(t):
    if m.group(1).strip().lower() not in {"changeme","change-me","your-token","your_password","placeholder","example","null","none"}: out.append(r.as_posix()); break
 return sorted(set(out))
def push(b,dry):
 git(["add","-A"]); n=git(["diff","--cached","--name-status"]).stdout.splitlines(); log(f"changes: {len(n)}")
 if not n:return False
 if dry: log("dry-run: commit/push skipped"); return True
 git(["config","user.name","HA File Sync Bridge"]); git(["config","user.email","ha-file-sync-bridge@localhost"]); git(["commit","-m",f"Sync Home Assistant /config - {now()}"]); h=git(["rev-parse","HEAD"]).stdout.strip(); log(f"commit: {h[:8]}"); git(["push","origin",b]); git(["fetch","--prune","origin"])
 if git(["rev-parse",f"origin/{b}"]).stdout.strip()!=h: raise RuntimeError("push verification failed")
 log("push: OK"); return True
def state(): return load(STATE,{})
def status(**v):
 x=load(STATUS,{"version":VERSION}); x.update(v); x.update(version=VERSION,updated_at=now()); save(STATUS,x)
def acquire():
 DATA.mkdir(parents=True,exist_ok=True)
 try:
  fd=os.open(LOCK,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.write(fd,f"pid={os.getpid()}\n".encode()); os.close(fd)
 except FileExistsError: raise RuntimeError("sync already running")
def release():
 try:LOCK.unlink()
 except FileNotFoundError:pass
def sync(c,forced=None):
 acquire()
 try:
  status(state="running",error=None); b=str(c.get("branch","main")); dry=bool(c.get("dry_run",False)); mode=forced or str(c.get("sync_mode","bidirectional")); log("sync start"); repo(str(c["config_repo"]),b)
  if git(["fsck","--no-progress"],check=False).returncode: raise RuntimeError("repository invalid")
  log("repo: OK"); s=state(); last=s.get("last_sync_commit"); lc=treehash(CONFIG,c)!=s.get("last_config_hash"); git(["fetch","--prune","origin"]); rh=git(["rev-parse",f"origin/{b}"]).stdout.strip(); rc=bool(last and rh!=last)
  if last and lc and rc: raise RuntimeError("SYNC CONFLICT: both sides changed")
  if mode=="git_to_ha" or (mode=="bidirectional" and rc and not lc):
   git(["reset","--hard",f"origin/{b}"]); snapshot(c); log(f"GitHub -> /config: remote prepared")
   for p in list(WORK.iterdir()):
    if p.name==".git": continue
   # WORK is already checked out at origin after reset; copy manually
   names={p.name for p in WORK.iterdir() if p.name!=".git" and not excluded(p.name,c)}
   for p in list(CONFIG.iterdir()):
    if not excluded(p.name,c) and p.name not in names: shutil.rmtree(p) if p.is_dir() else p.unlink()
   for p in WORK.iterdir():
    if p.name==".git" or excluded(p.name,c): continue
    d=CONFIG/p.name; shutil.rmtree(d) if d.exists() and d.is_dir() else (d.unlink() if d.exists() else None); shutil.copytree(p,d,ignore=ignore(c)) if p.is_dir() else shutil.copy2(p,d)
  elif mode in {"ha_to_git","bidirectional"} and (lc or not last):
   if tracked_sensitive(): raise RuntimeError("SECURITY BLOCKED: tracked sensitive path")
   snapshot(c); log(f"/config prepared: {to_work(c)} items"); f=secret_scan(c)
   if f: raise RuntimeError(f"SECRET SCAN BLOCKED: {len(f)} finding(s)")
   push(b,dry)
  h=git(["rev-parse","HEAD"]).stdout.strip(); save(STATE,{**state(),"last_sync_commit":h,"last_config_hash":treehash(CONFIG,c),"last_success":now()}); status(state="idle",last_sync=h,error=None); log("sync complete")
 except Exception as e: status(state="error",error=str(e)); log(f"ERROR: {e}")
 finally: release()
def restore(name,c,dry=False):
 s=SNAPSHOTS/name
 if not s.is_dir(): raise RuntimeError("snapshot not found")
 if not dry:
  snapshot(c)
  for p in list(CONFIG.iterdir()):
   if not excluded(p.name,c): shutil.rmtree(p) if p.is_dir() else p.unlink()
  for p in s.iterdir():
   if excluded(p.name,c):continue
   d=CONFIG/p.name; shutil.copytree(p,d,ignore=ignore(c)) if p.is_dir() else shutil.copy2(p,d)
 log(f"snapshot restore {'planned' if dry else 'completed'}: {name}")
class H(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def out(self,n,x):
  b=json.dumps(x).encode(); self.send_response(n); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_GET(self):
  if self.path=="/status":self.out(200,load(STATUS,{"version":VERSION}));return
  if self.path=="/snapshots":self.out(200,{"snapshots":sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir()) if SNAPSHOTS.exists() else []});return
  self.out(200,{"service":"HA File Sync Bridge","version":VERSION,"status":load(STATUS,{})}) if self.path=="/" else self.out(404,{"error":"not found"})
 def do_POST(self):
  try:
   l=int(self.headers.get("Content-Length","0")); body=json.loads(self.rfile.read(l) or b"{}") if l else {}
   if self.path in {"/sync","/sync/bidirectional","/sync/ha_to_git","/sync/git_to_ha"}:
    m="ha_to_git" if self.path.endswith("ha_to_git") else ("git_to_ha" if self.path.endswith("git_to_ha") else None); threading.Thread(target=lambda:sync(cfg(),m),daemon=True).start(); self.out(202,{"accepted":True});return
   if self.path=="/restore": restore(str(body.get("snapshot","")),cfg(),bool(body.get("dry_run",False))); self.out(200,{"ok":True});return
   self.out(404,{"error":"not found"})
  except Exception as e:self.out(400,{"error":str(e)})
def main():
 DATA.mkdir(parents=True,exist_ok=True); status(state="idle",error=None); log(f"HA File Sync Bridge {VERSION}"); http=http.server.ThreadingHTTPServer(("0.0.0.0",PORT),H)
 def worker():
  first=True
  while True:
   try:
    c=cfg(); m=str(c.get("initial_sync","ha_to_git")) if first and not state().get("last_sync_commit") else None; sync(c,m)
   except Exception:pass
   first=False; time.sleep(max(10,int(cfg().get("poll_interval",60))))
 threading.Thread(target=worker,daemon=True).start(); http.serve_forever()
if __name__=="__main__":main()
