"""
install_tars_background_startup.py
-----------------------------------
Registers (or removes) a Windows Task Scheduler entry that launches
TARS in headless background mode automatically at every user logon.

USAGE
    python install_tars_background_startup.py           # install
    python install_tars_background_startup.py --remove  # uninstall
    python install_tars_background_startup.py --status  # query

The installer:
  * Works for both source (python tars_background.py) and packaged
    (TARS_Background.exe) modes -- auto-detects which is available.
  * Correctly handles paths that contain spaces.
  * Prevents duplicate Task Scheduler entries.
  * Does NOT require admin rights (task runs in the current user session).
  * Configures the task to restart automatically if TARS crashes.
  * Does NOT modify any existing startup entry for the GUI TARS.

Target platform: Windows 10 / Windows 11
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


# ── Names ──────────────────────────────────────────────────────────────────────
TASK_NAME    = "TARS_Background_Startup"
TASK_COMMENT = "TARS personal AI assistant -- silent background listener"


def _get_base_dir() -> Path:
    """Resolve the TARS project directory regardless of CWD."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _find_launcher() -> tuple:
    """Return (executable_str, args_list) for launching tars_background.

    Priority:
      1. TARS_Background.exe  -- packaged, windowless, no Python needed
      2. pythonw.exe + tars_background.py  -- source, no console window
      3. python.exe  + tars_background.py  -- fallback (console visible)
    """
    base = _get_base_dir()

    # 1. Packaged executable
    exe_pkg = base / "TARS_Background.exe"
    if exe_pkg.exists():
        return str(exe_pkg), []

    # 2. pythonw (no console window)
    bg_script = base / "tars_background.py"
    if not bg_script.exists():
        print(
            "ERROR: tars_background.py not found at " + str(bg_script) + "\n"
            "Make sure you are running this installer from the TARS project folder."
        )
        sys.exit(1)

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw), [str(bg_script)]

    # 3. Fallback: python.exe (console may be visible)
    print(
        "WARNING: pythonw.exe not found next to python.exe. "
        "A console window may appear briefly at logon."
    )
    return sys.executable, [str(bg_script)]


def _build_tr(exe: str, args: list) -> str:
    """Build the /TR value for schtasks -- quoted exe + quoted args."""
    if not args:
        return '"' + exe + '"'
    quoted_args = " ".join('"' + a + '"' for a in args)
    return '"' + exe + '" ' + quoted_args


def _schtasks(*args, capture: bool = True) -> subprocess.CompletedProcess:
    """Run schtasks.exe and return the result."""
    cmd = ["schtasks"] + list(args)
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _task_exists() -> bool:
    """Return True if the TARS background task is already registered."""
    r = _schtasks("/Query", "/TN", TASK_NAME)
    return r.returncode == 0


def install() -> None:
    """Register the Task Scheduler entry."""
    if _task_exists():
        print(
            "Task '" + TASK_NAME + "' is already registered.\n"
            "Nothing to do.  (Run with --remove then re-run to replace it.)"
        )
        return

    exe, args = _find_launcher()
    base_dir  = _get_base_dir()
    tr        = _build_tr(exe, args)

    import datetime
    today = datetime.date.today().strftime("%m/%d/%Y")

    cmd = [
        "schtasks", "/Create",
        "/TN",  TASK_NAME,
        "/TR",  tr,
        "/SC",  "ONLOGON",
        "/IT",            # interactive: needs user desktop (mic, audio)
        "/F",             # overwrite if already exists (idempotent)
        "/SD",  today,
        "/ST",  "00:00",
        "/DELAY", "0000:30",    # 30-second delay after logon
        "/RL",  "LIMITED",      # run as normal user (NOT elevated)
    ]

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print("ERROR: schtasks /Create failed:\n" + (r.stderr or r.stdout))
        sys.exit(1)

    # Patch task XML to add restart-on-crash (no schtasks flag for this).
    _patch_restart_on_crash()

    # Record the working directory so the app finds config/ etc.
    wdir_file = base_dir / ".tars_bg_workdir"
    wdir_file.write_text(str(base_dir), encoding="utf-8")

    print("Task '" + TASK_NAME + "' registered successfully.")
    print("  Launcher : " + tr)
    print("  Trigger  : At logon for current user")
    print("  Delay    : 30 seconds after logon")
    print("  Restart  : Yes (on crash, up to 3 times, 60 s delay)")
    print()
    print("TARS background will start automatically the next time you log in.")
    print("To start it NOW (without rebooting):  python tars_background.py")


def _patch_restart_on_crash() -> None:
    """Export the task XML, add <RestartOnFailure>, reimport.

    schtasks /Create has no restart-on-failure flag, but the XML schema
    supports it under <Settings>.
    """
    import tempfile
    import xml.etree.ElementTree as ET

    tmp_path = None
    try:
        r = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
        if r.returncode != 0:
            print("WARNING: Could not export task XML for restart patch: " + r.stderr)
            return

        xml_str = r.stdout
        NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        ET.register_namespace("", NS)

        root = ET.fromstring(xml_str)
        ns   = {"t": NS}

        settings = root.find("t:Settings", ns)
        if settings is None:
            settings = ET.SubElement(root, "{" + NS + "}Settings")

        rof = settings.find("t:RestartOnFailure", ns)
        if rof is None:
            rof = ET.SubElement(settings, "{" + NS + "}RestartOnFailure")
        rof.set("Count", "3")
        rof.set("Interval", "PT1M")

        patched = ET.tostring(root, encoding="unicode", xml_declaration=False)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".xml", delete=False, encoding="utf-16"
        ) as tmp:
            tmp.write('<?xml version="1.0" encoding="UTF-16"?>\n')
            tmp.write(patched)
            tmp_path = tmp.name

        ri = _schtasks("/Create", "/TN", TASK_NAME, "/XML", tmp_path, "/F")
        if ri.returncode != 0:
            print("WARNING: Restart-on-crash patch import failed: " + ri.stderr)
        else:
            print("  Restart on crash: 3 attempts, 60 s interval.")
    except Exception as exc:
        print("WARNING: Could not patch restart behaviour: " + str(exc))
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def remove() -> None:
    """Remove the Task Scheduler entry."""
    if not _task_exists():
        print("Task '" + TASK_NAME + "' is not registered.  Nothing to remove.")
        return

    r = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if r.returncode != 0:
        print("ERROR: schtasks /Delete failed:\n" + (r.stderr or r.stdout))
        sys.exit(1)

    base_dir  = _get_base_dir()
    wdir_file = base_dir / ".tars_bg_workdir"
    if wdir_file.exists():
        wdir_file.unlink(missing_ok=True)

    print("Task '" + TASK_NAME + "' removed.")
    print("TARS background will no longer start automatically at logon.")


def status() -> None:
    """Print current registration status."""
    if _task_exists():
        r = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
        print(r.stdout)
    else:
        print("Task '" + TASK_NAME + "' is NOT registered.")


def _check_windows() -> None:
    import platform
    if platform.system() != "Windows":
        print("ERROR: Windows auto-start via Task Scheduler is Windows-only.")
        print("  Linux: add tars_background.py to ~/.config/autostart/ or a systemd user service.")
        print("  macOS: add a launchd plist to ~/Library/LaunchAgents/.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install or remove TARS background auto-start (Windows Task Scheduler)."
    )
    parser.add_argument(
        "--remove", action="store_true",
        help="Remove the auto-start task instead of installing it."
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current Task Scheduler registration status."
    )
    args = parser.parse_args()

    _check_windows()

    if args.remove:
        remove()
    elif args.status:
        status()
    else:
        install()


if __name__ == "__main__":
    main()
