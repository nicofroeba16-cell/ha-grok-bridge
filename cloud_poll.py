#!/usr/bin/env python3
"""Poller: command.json per Git-SSH. Whitelist + docker exec."""
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

OWNER = "nicofroeba16-cell"
REPO = "ha-grok-bridge"
HOME = Path("/home/vboxuser/grok-agent")
LAST_ID = HOME / "last_id"
RESULT_LOCAL = HOME / "result.json"
CLONE = HOME / "bridge-push"
INTERVAL = 20
CONTAINER = os.environ.get("HA_CONTAINER", "homeassistant")

ALLOWED_PREFIXES = (
    "ha core info",
    "ha core check",
    "ha core restart",
    "git -C /config ",
    "bash /config/deploy.sh",
    "python3 /config/apply_updates.py",
    "mkdir -p /config/",
    "cp -f /config/",
)


def log(msg):
    print(msg, flush=True)


def allowed(cmd):
    c = cmd.strip()
    parts = [p.strip() for p in c.replace("&&", ";").split(";") if p.strip()]
    if not parts:
        return False
    for p in parts:
        if p.startswith("true") or p.startswith("echo "):
            continue
        if not any(p.startswith(pre) or p == pre.rstrip() for pre in ALLOWED_PREFIXES):
            return False
    return True


def fetch_command():
    try:
        if not CLONE.exists():
            subprocess.run(
                ["git", "clone", "--depth", "1",
                 "git@github.com:%s/%s.git" % (OWNER, REPO), str(CLONE)],
                check=True, capture_output=True, text=True, timeout=60,
            )
        else:
            subprocess.run(
                ["git", "-C", str(CLONE), "fetch", "origin", "main"],
                capture_output=True, text=True, timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(CLONE), "checkout", "origin/main", "--", "command.json"],
                capture_output=True, text=True, timeout=30,
            )
        payload = json.loads((CLONE / "command.json").read_text())
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        log("[-] command.json: %s" % exc)
        return None
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("command"):
        return None
    return payload


def _shell(cmd):
    proc = subprocess.run(
        cmd, shell=True, text=True, capture_output=True, timeout=180, cwd=str(HOME),
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out.strip()


def run_command(cmd):
    if not allowed(cmd):
        return 1, "ABGEBROCHEN: nicht in der Whitelist!"
    if Path("/config").is_dir():
        return _shell(
            "mkdir -p ~/.ssh && ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null; " + cmd
        )
    inner = "docker exec %s bash -lc %s" % (CONTAINER, shlex.quote(cmd))
    code, out = _shell(inner)
    if "No such container" in out:
        return _shell("docker exec hassio_cli bash -lc %s" % shlex.quote(cmd))
    return code, out


def write_result(entry):
    RESULT_LOCAL.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
    try:
        dest = CLONE / "result.json"
        dest.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
        subprocess.run(["git", "-C", str(CLONE), "add", "result.json"], check=False)
        st = subprocess.run(
            ["git", "-C", str(CLONE), "commit", "-m", "result %s" % entry.get("id")],
            capture_output=True, text=True,
        )
        if st.returncode == 0:
            subprocess.run(
                ["git", "-C", str(CLONE), "push", "origin", "main"],
                capture_output=True, text=True, timeout=60,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        log("[-] result.json push: %s" % exc)


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    last = LAST_ID.read_text().strip() if LAST_ID.exists() else ""
    log("Ueberwache command.json per Git-SSH...")
    while True:
        payload = fetch_command()
        if payload:
            cid = str(payload["id"])
            if cid != last:
                cmd = str(payload["command"])
                log("[+] Neuer Befehl empfangen (ID %s): %s" % (cid, cmd))
                code, out = run_command(cmd)
                log("[+] Ergebnis:")
                log(out or "(leer)")
                write_result({
                    "id": cid,
                    "command": cmd,
                    "exit_code": code,
                    "ok": code == 0,
                    "output": out,
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                LAST_ID.write_text(cid + "\n")
                last = cid
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
