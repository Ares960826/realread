"""Get the user's attention when RealRead is waiting for them (bot check, login)."""

from __future__ import annotations

import shutil
import subprocess
import sys


def _quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def desktop(title: str, message: str) -> None:
    """Best-effort desktop notification plus a terminal bell; never raises."""
    try:
        if sys.platform == "darwin":
            script = f'display notification "{_quote(message)}" with title "{_quote(title)}" sound name "Glass"'
            subprocess.run(["osascript", "-e", script], timeout=5, check=False, capture_output=True)
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, message], timeout=5, check=False)
        elif sys.platform.startswith("win"):
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                f'$n.Visible=$true;$n.ShowBalloonTip(10000,"{_quote(title)}","{_quote(message)}",'
                "[System.Windows.Forms.ToolTipIcon]::Warning);Start-Sleep 10;$n.Dispose()"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        sys.stderr.write("\a")
        sys.stderr.flush()
    except Exception:
        pass
