"""
tars_background.py -- TARS headless background mode.

Entry point for the invisible, always-on background process.
Starts TARS without opening any GUI window.  Runs the local
wake-word detector continuously.  When "TARS" is heard,
the existing tarsLive engine is already connected and wakes.
When the idle timeout fires (existing _run_sleep_watch) TARS
goes back to dormant/sleeping mode but the wake detector stays
active.

USAGE
    python tars_background.py          # foreground (development)
    pythonw tars_background.py         # no console window (manual)
    TARS_Background.exe                # packaged, windowless

WINDOWS AUTO-START
    python install_tars_background_startup.py   # register Task Scheduler
    python install_tars_background_startup.py --remove   # unregister

NORMAL GUI MODE (unchanged)
    python main.py
"""

# ============================================================
# 0. MUST BE FIRST: suppress console windows for subprocesses
# ============================================================
import platform as _platform
import subprocess as _subprocess

if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = (
                kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            )
            kw.pop("startupinfo", None)
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen


# ============================================================
# 1. Console UTF-8 (for log output; same pattern as main.py)
# ============================================================
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ============================================================
# 2. Standard imports
# ============================================================
import asyncio
import ctypes
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path


# ============================================================
# 3. Resolve base directory (works frozen / unfrozen)
# ============================================================
def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = _get_base_dir()


# ============================================================
# 4. Logging -- background log file only, no console noise
# ============================================================
_LOG_DIR = BASE_DIR / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "tars_background.log"

_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_handler)
# Also echo to stderr when running interactively (pythonw has no console).
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
logging.getLogger().addHandler(_stderr_handler)

_log = logging.getLogger("tars.background")


# Redirect print() to the log so tarsLive status lines appear in the file.
class _LogStream:
    """Forward print() output to the background log."""

    def __init__(self, level: int = logging.INFO) -> None:
        self._level = level
        self._buf   = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                logging.log(self._level, "%s", line)
        return len(s)

    def flush(self) -> None:
        pass

# When running under pythonw or as a frozen windowless exe there is no real
# stdout/stderr -- redirect so nothing is lost.
try:
    if sys.stdout is None:
        raise AttributeError
    sys.stdout.fileno()   # raises if not a real fd (pythonw / frozen)
except Exception:
    sys.stdout = _LogStream(logging.INFO)   # type: ignore[assignment]
    sys.stderr = _LogStream(logging.WARNING) # type: ignore[assignment]


# ============================================================
# 5. Duplicate-instance protection (Windows named mutex)
# ============================================================
_MUTEX_NAME = "Global\\TARS_Background_SingleInstance"
_mutex_handle = None   # keep a reference so GC does not close it


def _acquire_singleton() -> bool:
    """Return True if this process is the first background instance.

    Uses a named Win32 mutex on Windows; falls back to a lock file on POSIX.
    The mutex is intentionally kept open for the lifetime of the process.
    """
    global _mutex_handle
    if _platform.system() == "Windows":
        try:
            _mutex_handle = ctypes.windll.kernel32.CreateMutexW(
                None, True, _MUTEX_NAME
            )
            # ERROR_ALREADY_EXISTS == 183
            last_err = ctypes.windll.kernel32.GetLastError()
            if last_err == 183:
                return False
            return bool(_mutex_handle)
        except Exception as exc:
            _log.warning("Mutex check failed (%s) -- assuming no duplicate.", exc)
            return True
    else:
        lock = BASE_DIR / "tars_background.lock"
        try:
            import fcntl
            _f = open(lock, "w")          # noqa: SIM115  (kept open on purpose)
            fcntl.flock(_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


# ============================================================
# 6. Main runner
# ============================================================

def _run_background() -> None:
    """Check config, build HeadlessUI, start tarsLive.

    tarsLive already handles:
      * Gemini connection + reconnect loop (exponential backoff)
      * Local wake-word detector (_wake_detector, _ensure_wake_detector)
      * Wake/sleep state machine (_awake, _wake_enabled, _run_sleep_watch)
      * Auto-sleep after idle timeout (WAKE_SLEEP_TIMEOUT = 120 s)
      * Session resumption
      * Error recovery

    All this function does is:
      1. Verify the API key is present (exit with a log entry if not)
      2. Ensure wake word is enabled in config
      3. Verify/download the wake-word model
      4. Construct HeadlessUI + tarsLive and run the async loop
    """
    _log.info("=" * 60)
    _log.info("TARS background mode starting  (PID %d)", os.getpid())
    _log.info("Base dir: %s", BASE_DIR)
    _log.info("Log file: %s", _LOG_FILE)

    # -- 6a. API key check ----------------------------------------------------
    # sys.path must include BASE_DIR so project imports work.
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    from memory.config_manager import (
        get_wake_word_enabled,
        is_configured,
        save_wake_word_enabled,
    )

    if not is_configured():
        _log.error(
            "Gemini API key not found in config/api_keys.json.  "
            "Run  python main.py  to configure TARS, then restart "
            "the background process.  Exiting."
        )
        return

    _log.info("API key present.")

    # -- 6b. Ensure wake word is enabled in config ----------------------------
    if not get_wake_word_enabled():
        _log.info(
            "Wake word was disabled in config -- enabling it now "
            "(required for background mode)."
        )
        save_wake_word_enabled(True)

    # -- 6c. Verify / download the wake-word model ----------------------------
    from core.wake_word import install_and_download, is_ready as wake_is_ready

    if not wake_is_ready():
        _log.warning("openwakeword model not yet downloaded -- attempting one-time download...")
        ok, msg = install_and_download(
            logger=lambda m: _log.info("[WakeSetup] %s", m),
            notify=lambda m: _log.info("[WakeSetup] %s", m),
        )
        if not ok:
            _log.error(
                "Wake-word model download failed: %s  "
                "Run  python main.py , go to Settings > Wake Word, "
                "and press Download.  Then restart the background process.  "
                "Exiting.",
                msg,
            )
            return
        _log.info("Wake-word model ready.")
    else:
        _log.info("Wake-word model verified.")

    # -- 6d. Build HeadlessUI + tarsLive ------------------------------------
    from core.headless_ui import HeadlessUI
    from main import tarsLive

    ui     = HeadlessUI()
    tars = tarsLive(ui)

    _log.info(
        "Engine constructed.  Connecting to Gemini...  "
        "Say 'TARS' to wake assistant after connection."
    )

    # -- 6e. Run the async event loop (tarsLive.run never returns normally) -
    try:
        asyncio.run(tars.run())
    except KeyboardInterrupt:
        _log.info("KeyboardInterrupt -- shutting down.")
    except SystemExit:
        _log.info("SystemExit -- shutting down.")
    except Exception as exc:
        _log.exception("Unhandled exception in background engine: %s", exc)
    finally:
        _log.info("TARS background process exiting.")


def main() -> None:
    # ── Singleton guard ───────────────────────────────────────────────────────
    if not _acquire_singleton():
        _log.warning(
            "Another TARS background instance is already running "
            "(named mutex already held).  Exiting cleanly."
        )
        sys.exit(0)

    _run_background()


if __name__ == "__main__":
    main()
