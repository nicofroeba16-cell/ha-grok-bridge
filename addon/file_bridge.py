#!/usr/bin/env python3
"""HA File Sync Bridge 0.5.1.

Safe Git-backed synchronisation core.  The live HA configuration is never
modified automatically: the bridge maintains a local Git working tree and
provides snapshot/validation/deploy/rollback/status primitives for callers.
"""
from __future__ import annotations
import json, shutil, subprocess, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA = Path('/data')
CONFIG = Path('/config')
WORK = DATA / 'bridge-work'
SNAPSHOTS = DATA / 'snapshots'
OPTIONS = DATA / 'options.json'

def now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def log(message: str) -> None:
    print(f'[file-bridge] {message}', flush=True)

def run_git(args: list[str], cwd: Path = WORK, check: bool = True) -> subprocess.CompletedProcess[str]:
    log(f'git command: git {" ".join(args)}')
    return subprocess.run(['git', *args], cwd=cwd, check=check, capture_output=True,
                          text=True, timeout=180)

def load_options() -> dict[str, Any]:
    defaults = {'poll_interval': 60,
                'config_repo': 'git@github.com:nicofroeba16-cell/ha-grok-bridge-live.git'}
    if OPTIONS.is_file():
        try:
            value = json.loads(OPTIONS.read_text())
            if isinstance(value, dict): defaults.update(value)
        except Exception as exc:
            log(f'options warning: {exc}')
    if not defaults.get('config_repo'): raise ValueError('config_repo is required')
    return defaults

def ensure_repo(repo_url: str) -> None:
    WORK.parent.mkdir(parents=True, exist_ok=True)
    if not (WORK / '.git').is_dir():
        if WORK.exists(): shutil.rmtree(WORK)
        p = run_git(['clone', '--no-checkout', repo_url, str(WORK)], cwd=DATA)
    else:
        run_git(['remote', 'set-url', 'origin', repo_url])
        p = run_git(['fetch', '--prune', 'origin'])
    if p.stderr.strip(): log(f'git stderr: {p.stderr.strip()}')

def snapshot() -> Path:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    target = SNAPSHOTS / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if CONFIG.is_dir(): shutil.copytree(CONFIG, target)
    else: target.mkdir()
    return target

def validate() -> tuple[bool, str]:
    if not (WORK / '.git').is_dir(): return False, 'repository not initialized'
    p = run_git(['fsck', '--no-progress'], check=False)
    if p.returncode != 0: return False, p.stderr.strip() or 'git fsck failed'
    return True, 'repository valid'

def deploy(source: Path | None = None) -> None:
    source = source or WORK
    if not source.is_dir(): raise FileNotFoundError(source)
    snapshot()
    CONFIG.mkdir(parents=True, exist_ok=True)
    excluded = {'.git'}
    for item in source.iterdir():
        if item.name in excluded: continue
        destination = CONFIG / item.name
        if item.is_dir():
            if destination.exists(): shutil.rmtree(destination)
            shutil.copytree(item, destination)
        else: shutil.copy2(item, destination)

def rollback(snapshot_path: Path) -> None:
    if not snapshot_path.is_dir(): raise FileNotFoundError(snapshot_path)
    if CONFIG.exists():
        for item in CONFIG.iterdir():
            if item.name == '.storage': continue
            if item.is_dir(): shutil.rmtree(item)
            else: item.unlink()
    for item in snapshot_path.iterdir():
        destination = CONFIG / item.name
        if item.is_dir(): shutil.copytree(item, destination)
        else: shutil.copy2(item, destination)

def status() -> dict[str, Any]:
    result: dict[str, Any] = {'time': now(), 'repository': str(WORK), 'config': str(CONFIG)}
    if (WORK / '.git').is_dir():
        p = run_git(['status', '--short'], check=False)
        result['git_status'] = p.stdout.splitlines()
    else: result['git_status'] = None
    result['snapshots'] = len(list(SNAPSHOTS.iterdir())) if SNAPSHOTS.is_dir() else 0
    return result

def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            options = load_options()
            ensure_repo(options['config_repo'])
            ok, message = validate()
            log(message)
            if ok: log('repository access OK')
        except Exception as exc:
            log(f'request failed: {exc}')
        time.sleep(int(load_options()['poll_interval']))

if __name__ == '__main__': main()
