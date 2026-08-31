#!/usr/bin/env python3
"""Pollt command.json über die GitHub-API (kein Raw-5-Min-Cache).
Schreibt result.json lokal und nach ha-grok-bridge (SSH-Git).
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER = "nicofroeba16-cell"
REPO = "ha-grok-bridge"
API = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/command.json?ref=main"
HOME = Path("/home/vboxuser/grok-agent")
LAST_ID = HOME / "last_id"
RESULT_LOCAL = HOME / "result.json"
PUSH_DIR = HOME / "bridge-push"
INTERVAL = 15

UA = {"User-Agent": "ha-grok-bridge", "Accept": "application/vnd.github+json"}


def log(msg: str) -> None:
    print(msg, flush=True)


def api_get(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_command() -> dict | None:
    try:
        data = api_get(API)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log(f"[-] command.json: {exc}")
        return None
    raw = data.get("content") or ""
    try:
        text = base64.b64decode(raw.replace("\n", "")).decode()
        payload = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        log(f"[-] parse: {exc}")
        return None
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("command"):
        return None
    return payload


def run_command(cmd: str) -> tuple[int, str]:
    wrapped = (
        "mkdir -p ~/.ssh && "
        "ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null; "
        + cmd
    )
    proc = subprocess.run(
        wrapped,
        shell=True,
        text=True,
        capture_output=True,
        timeout=180,
        cwd=str(HOME),
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out.strip()


def write_result(entry: dict) -> None:
    RESULT_LOCAL.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
    try:
        if not PUSH_DIR.exists():
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    f"git@github.com:{OWNER}/{REPO}.git",
                    str(PUSH_DIR),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            subprocess.run(
                ["git", "-C", str(PUSH_DIR), "pull", "--ff-only", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        dest = PUSH_DIR / "result.json"
        dest.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
        subprocess.run(["git", "-C", str(PUSH_DIR), "add", "result.json"], check=False)
        st = subprocess.run(
            ["git", "-C", str(PUSH_DIR), "commit", "-m", f"result {entry.get('id')}"],
            capture_output=True,
            text=True,
        )
        if st.returncode == 0:
            subprocess.run(
                ["git", "-C", str(PUSH_DIR), "push", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=60,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"[-] result.json push: {exc}")


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    last = LAST_ID.read_text().strip() if LAST_ID.exists() else ""
    log("Überwache GitHub-API auf neue Befehle...")
    while True:
        payload = fetch_command()
        if payload:
            cid = str(payload["id"])
            if cid != last:
                cmd = str(payload["command"])
                log(f"[+] Neuer Befehl empfangen (ID {cid}): {cmd}")
                code, out = run_command(cmd)
                log("[+] Ergebnis:")
                log(out or "(leer)")
                entry = {
                    "id": cid,
                    "command": cmd,
                    "exit_code": code,
                    "ok": code == 0,
                    "output": out,
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                write_result(entry)
                LAST_ID.write_text(cid + "\n")
                last = cid
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
