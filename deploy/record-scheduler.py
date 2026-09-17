"""Record observed timer activity; contains no credentials or command output."""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

jobs = ("collect", "publish", "settle", "watchdog", "dashboard", "backup")
active = all(
    subprocess.run(["systemctl", "is-active", "--quiet", f"power-forecast-{job}.timer"]).returncode
    == 0
    for job in jobs
)
path = Path("/var/lib/power-forecast/operations/scheduler.json")
path.parent.mkdir(parents=True, exist_ok=True)
os.chown(path.parent, 1000, 1000)
fd, temporary = tempfile.mkstemp(dir=path.parent)
with os.fdopen(fd, "w") as output:
    json.dump({"active": active, "checked_at": datetime.now(timezone.utc).isoformat()}, output)
os.chown(temporary, 1000, 1000)
os.replace(temporary, path)
