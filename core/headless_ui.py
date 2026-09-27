"""
core/headless_ui.py -- headless UI adapter for background mode.

Implements ONLY the attributes and methods that JarvisLive actually accesses
on its `ui` object (determined by auditing every `self.ui.` reference in
main.py).  Everything visual is a no-op; logs go to the background log file
via Python's logging module.

This object is intentionally minimal.  Do not add methods that the real
JarvisUI has but that JarvisLive never calls -- that would only create a
maintenance burden.

Methods called by JarvisLive.__init__ (attributes set ON ui):
    on_push_to_talk, ptt_hold, on_text_command, on_remote_clicked,
    on_interrupt, on_voice_change, on_audio_device_change,
    get_plugins, get_plugin_settings, request_say,
    wake_is_ready, wake_get_state, on_wake_toggle, on_wake_manual,
    on_wake_install

Attributes READ by JarvisLive:
    ui.muted           -- bool, always False (never muted in headless mode)
    ui.current_file    -- None (no file upload in headless mode)
    ui._win._ready     -- used in API-key reconfig loop; always True here so
                         the loop exits immediately after logging the error

Methods CALLED by JarvisLive on ui:
    set_state(state)
    write_log(msg)
    set_audio_level(level)
    push_visemes(frames, hop, at)
    show_content(label, text)
    start_camera_stream()
    stop_camera_stream()
    show_confirm(title, detail)
    hide_confirm()
    notify_phone_connected()
    prompt_reconfig()
    wait_for_api_key()       -- not called by JarvisLive but by the runner
"""
from __future__ import annotations

import logging

# Module-level logger.  The background entry point configures the handler;
# this module just uses the named logger so the caller decides where it goes.
_log = logging.getLogger("tars.background")


class _FakeWin:
    """Stands in for ui._win so the API-key reconfig wait-loop terminates."""
    _ready = True


class HeadlessUI:
    """Drop-in replacement for JarvisUI in background/headless mode."""

    # -- Attributes JarvisLive sets on ui (callbacks) -------------------------
    on_push_to_talk:        object = None
    ptt_hold:               object = None
    on_text_command:        object = None
    on_remote_clicked:      object = None
    on_interrupt:           object = None
    on_voice_change:        object = None
    on_audio_device_change: object = None
    get_plugins:            object = None
    get_plugin_settings:    object = None
    request_say:            object = None
    wake_is_ready:          object = None
    wake_get_state:         object = None
    on_wake_toggle:         object = None
    on_wake_manual:         object = None
    on_wake_install:        object = None

    # -- Attributes READ by JarvisLive ----------------------------------------
    muted        = False   # headless is never muted
    current_file = None    # no file-upload in headless mode

    def __init__(self) -> None:
        self._win = _FakeWin()
        self._state = "SLEEPING"

    # -- State display ---------------------------------------------------------

    def set_state(self, state: str) -> None:
        """Track state internally; log transitions so the log is readable."""
        if state != self._state:
            _log.debug("State: %s -> %s", self._state, state)
            self._state = state

    # -- Log output ------------------------------------------------------------

    def write_log(self, msg: str) -> None:
        """Route all JarvisLive log messages to the background log file."""
        msg = str(msg).strip()
        if not msg:
            return
        if msg.startswith("ERR:"):
            _log.error("%s", msg)
        elif msg.startswith("NET:"):
            _log.warning("%s", msg)
        else:
            _log.info("%s", msg)

    # -- Audio visualisation -- all no-ops ------------------------------------

    def set_audio_level(self, level: float) -> None:
        pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        pass

    # -- Content panel -- no-op -----------------------------------------------

    def show_content(self, label: str, text: str) -> None:
        _log.debug("Content panel (%s) -- %d chars", label, len(text or ""))

    # -- Camera -- no-ops ------------------------------------------------------

    def start_camera_stream(self) -> None:
        pass

    def stop_camera_stream(self) -> None:
        pass

    # -- Confirmation gate -----------------------------------------------------
    # When _show_cb is None, core/confirm.py returns a "cannot confirm" message
    # to the model rather than performing the irreversible action.  So binding
    # a no-op here is safe: the model is told no confirmation is available.

    def show_confirm(self, title: str, detail: str) -> None:
        _log.warning(
            "Confirmation requested in headless mode (auto-denied): %s", title
        )

    def hide_confirm(self) -> None:
        pass

    # -- Phone / dashboard notifications ---------------------------------------

    def notify_phone_connected(self) -> None:
        _log.info("Phone connected via Remote Dashboard.")

    # -- API key / reconfig -- background mode logs and exits cleanly ----------

    def prompt_reconfig(self) -> None:
        """Called when the API key is invalid.

        In background mode there is no GUI to show a setup screen.  We log the
        problem clearly so the user knows to run `python main.py` and re-enter
        their key, then let the run loop pass (since _win._ready is always True
        here) so it can handle the retry logic.
        """
        _log.error(
            "API key is invalid or missing.  "
            "Run 'python main.py' to enter your Gemini API key, "
            "then restart the background process."
        )

    def wait_for_api_key(self) -> None:
        """In GUI mode this shows the setup screen and blocks.
        In background mode we just return; the runner checks is_configured()."""
        pass
