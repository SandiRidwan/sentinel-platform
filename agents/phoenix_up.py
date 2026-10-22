"""Start Phoenix, prove it answers, and keep it running for the parity check.

PowerShell's Start-Process could not reliably spawn it in this environment, and a server that
silently fails to start produces a `000` health check that looks like a firewall problem. This
script starts the process, waits for a real HTTP 200, prints the startup output if it never
arrives, and leaves it running.

    python _phoenix_up.py            # start (idempotent: reuses a server already answering)
    python _phoenix_up.py --stop     # stop the one it started
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 6006
BASE = f"http://127.0.0.1:{PORT}"
PID_FILE = Path(os.environ.get("TEMP", ".")) / "sentinel_phoenix.pid"


def healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def stop() -> int:
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        PID_FILE.unlink(missing_ok=True)
        print(f"stopped pid {pid}")
    return 0


def main() -> int:
    if "--stop" in sys.argv:
        return stop()

    if healthy():
        print(f"phoenix already answering on {BASE}")
        return 0

    venv = Path(__file__).resolve().parents[1] / ".venv" / "Scripts"
    env = {
        **os.environ,
        "PHOENIX_WORKING_DIR": str(Path(os.environ.get("TEMP", ".")) / "phoenix_data"),
        "PHOENIX_PORT": str(PORT),
        "PHOENIX_HOST": "127.0.0.1",
        "PHOENIX_DISABLE_AUTH": "true",
    }

    log = Path(os.environ.get("TEMP", ".")) / "phoenix_boot.log"
    handle = log.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(venv / "phoenix.exe"), "serve"],
        env=env, stdout=handle, stderr=subprocess.STDOUT,
    )
    PID_FILE.write_text(str(proc.pid))

    deadline = time.time() + 90
    while time.time() < deadline:
        if healthy():
            print(f"phoenix up on {BASE} (pid {proc.pid})")
            return 0
        if proc.poll() is not None:
            handle.close()
            print("phoenix exited during startup; last output:")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]))
            return 1
        time.sleep(2)

    handle.close()
    print("phoenix did not answer within 90s; last output:")
    print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
