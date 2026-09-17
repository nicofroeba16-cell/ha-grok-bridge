from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("ACC_PORT", "8877"))
    if not 1 <= port <= 65535:
        raise SystemExit("ACC_PORT must be between 1 and 65535")
    uvicorn.run("auto_control_center.app:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
