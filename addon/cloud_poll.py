#!/usr/bin/env python3
"""Add-on-Poller 0.2.3. Nur Git command.json, lokal /config. Keine HTTP-API."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("BRIDGE_HOME", "/data"))
LAST_ID = HOME / "last_id"
RESULT_LOCAL = HOME / "result.json"
CLONE = HOME / "bridge-push"
OPTIONS = Path("/data/options.json")
DEFAULT_REPO = "git@github.com:nicofroeba16-cell/ha-grok-bridge.git"
VERSION = "0.2.3"

ALLOWED_PREFIXES = (
    "ha info",
    "ha core info",
    "ha core check",
    "ha core stats",
    "ha core restart",
    "ha supervisor info",
    "ha host info",
    "ha resolution info",
    "git -C /config ",
    "bash /config/deploy.sh",
    "python3 /config/apply_updates.py",
    "mkdir -p /config/",
    "cp -f /config/",
)


def log(msg):
    print(msg, flush=True)


def load_options():
    opts = {"poll_interval": 15, "git_repo": DEFAULT_REPO}
    if OPTIONS.exists():
        try:
            raw = json.loads(OPTIONS.read_text())
            if isinstance(raw, dict):
                opts.update(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log("[-] options.json: %s" % exc)
    return opts


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


def _shell(cmd, cwd=None):
    proc = subprocess.run(
        cmd,
        shell=True,
        text=True,
        capture_output=True,
        timeout=180,
        cwd=cwd or str(HOME),
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out.strip()


def git_env():
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "ha-grok-bridge")
    env.setdefault("GIT_AUTHOR_EMAIL", "bridge@local")
    env.setdefault("GIT_COMMITTER_NAME", "ha-grok-bridge")
    env.setdefault("GIT_COMMITTER_EMAIL", "bridge@local")
    return env


def git_run(args, timeout=60, check=False):
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=git_env(),
    )
    err = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    if proc.returncode != 0:
        log("[-] git %s exit=%s %s" % (" ".join(args[1:6]), proc.returncode, err[:400]))
        if check:
            raise subprocess.CalledProcessError(proc.returncode, args, err)
    return proc.returncode, err


def git_clone_or_fetch(repo):
    if not CLONE.exists():
        git_run(["git", "clone", "--depth", "1", repo, str(CLONE)], check=True)
        return
    git_run(["git", "-C", str(CLONE), "fetch", "origin", "main"])
    git_run(["git", "-C", str(CLONE), "checkout", "origin/main", "--", "command.json"])


def fetch_command(repo):
    try:
        git_clone_or_fetch(repo)
        text = (CLONE / "command.json").read_text()
        payload = json.loads(text)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        log("[-] command.json: %s" % exc)
        return None
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("command"):
        return None
    return payload


def run_command(cmd):
    if not allowed(cmd):
        return 1, "ABGEBROCHEN: nicht in der Whitelist!"
    if not Path("/config").is_dir():
        return 1, "ABGEBROCHEN: /config fehlt im Add-on."
    return _shell(cmd)


def write_result(entry, repo):
    blob = json.dumps(entry, ensure_ascii=False, indent=2) + "\n"
    RESULT_LOCAL.write_text(blob)
    try:
        git_clone_or_fetch(repo)
        git_run(["git", "-C", str(CLONE), "pull", "--rebase", "--autostash", "origin", "main"])
        dest = CLONE / "result.json"
        dest.write_text(blob)
        git_run(["git", "-C", str(CLONE), "add", "result.json"])
        st, out = git_run(
            ["git", "-C", str(CLONE), "commit", "-m", "result %s" % entry.get("id")]
        )
        if st != 0 and "nothing to commit" in out.lower():
            log("[=] result.json unveraendert")
            return
        if st != 0:
            log("[-] result commit fail")
            return
        pst, _ = git_run(["git", "-C", str(CLONE), "push", "origin", "main"])
        if pst == 0:
            log("[+] result.json push ok via=%s" % entry.get("via"))
        else:
            log("[-] result.json push fail")
    except (subprocess.SubprocessError, OSError) as exc:
        log("[-] result.json push: %s" % exc)


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    opts = load_options()
    interval = int(opts.get("poll_interval") or 15)
    repo = str(opts.get("git_repo") or DEFAULT_REPO)
    last = LAST_ID.read_text().strip() if LAST_ID.exists() else ""
    log("Add-on-Poller %s · nur Git · kein HTTP · repo=%s · interval=%ss" % (VERSION, repo, interval))
    log("Kein SSH zum Host. /config lokal.")
    while True:
        payload = fetch_command(repo)
        if payload:
            cid = str(payload["id"])
            if cid != last:
                if not last:
                    LAST_ID.write_text(cid + "\n")
                    last = cid
                    log("[*] first boot: arm last_id=%s, skip exec" % cid)
                else:
                    cmd = str(payload["command"])
                    log("[+] Neuer Befehl (ID %s): %s" % (cid, cmd))
                    code, out = run_command(cmd)
                    log("[+] Ergebnis exit=%s" % code)
                    log(out or "(leer)")
                    write_result(
                        {
                            "id": cid,
                            "command": cmd,
                            "exit_code": code,
                            "ok": code == 0,
                            "output": out,
                            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "via": "addon-%s" % VERSION,
                        },
                        repo,
                    )
                    LAST_ID.write_text(cid + "\n")
                    last = cid
        time.sleep(interval)


if __name__ == "__main__":
    main()
