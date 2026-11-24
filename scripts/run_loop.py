"""Cross-platform 24/7 PAPER loop (Windows/macOS/Linux) - same behaviour as scripts/run_loop.sh.

One `python -m app.main trade --json` cycle every INTERVAL seconds (env, default 60).
Stop: create var/STOP (`start.sh stop` / `start.bat stop`), or Ctrl+C. PID in var/loop.pid.
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
(root / "var").mkdir(exist_ok=True)
(root / "logs").mkdir(exist_ok=True)
pidf, stop = root / "var" / "loop.pid", root / "var" / "STOP"
if pidf.exists():
    print(f"another loop may be running (var/loop.pid = {pidf.read_text().strip()}); delete the file if not")
    sys.exit(1)
interval = int(os.environ.get("INTERVAL", "60"))
pidf.write_text(str(os.getpid()))
stop.unlink(missing_ok=True)
print(f"ATLAS paper loop every {interval}s - logs/loop.log - stop: create var/STOP or Ctrl+C")
try:
    while not stop.exists():
        t0 = time.time()
        with open("logs/loop.log", "a") as out, open("logs/loop.err", "a") as err:
            rc = subprocess.call([sys.executable, "-m", "app.main", "trade", "--json"], stdout=out, stderr=err)
            if rc:
                err.write(f"{datetime.now().isoformat()} cycle failed rc={rc}\n")
        time.sleep(max(5, interval - (time.time() - t0)))
except KeyboardInterrupt:
    pass
finally:
    pidf.unlink(missing_ok=True)
