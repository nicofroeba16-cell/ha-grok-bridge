"""Bootstrap the AI control worker inside the add-on Python runtime."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

if os.environ.get("HA_GROK_AI_CONTROL_STARTED") != "1":
    worker = Path(__file__).with_name("ai_control.py")
    if worker.is_file():
        env = os.environ.copy()
        env["HA_GROK_AI_CONTROL_STARTED"] = "1"
        try:
            subprocess.Popen(
                ["python3", str(worker)],
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                env=env,
                close_fds=True,
            )
        except Exception:
            pass
