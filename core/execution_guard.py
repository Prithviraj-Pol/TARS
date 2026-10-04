"""
execution_guard.py — Lightweight Execution Guard and Tool Deduplication Layer for TARS.

Provides:
  - Tool-call ID tracking to reject repeated Gemini Live function call IDs.
  - Per-user-request action deduplication (ensures an action is executed only once
    per spoken user turn/command).
  - Cross-turn recent action cooldown for state-modifying actions.
  - Per-action in-progress execution locks to prevent concurrent executions of the same action.
  - Authoritative result state reporting.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional, Tuple

from core.app_verifier import (
    STATE_ALREADY_EXECUTED,
    STATE_DUPLICATE_IGNORED,
    STATE_LAUNCH_IN_PROGRESS,
    STATE_SUCCESS,
)

# Actions that modify system/filesystem state and require idempotency/cooldown guards
_STATEFUL_ACTIONS = {
    "open_app",
    "open_application",
    "open_file",
    "open_folder",
    "create_file",
    "create_folder",
    "desktop_control",
    "computer_control",
    "computer_settings",
    "close_camera",
    "shutdown_tars",
}


def _fingerprint_args(args: dict) -> str:
    """Produce a deterministic, normalized string representation of tool arguments."""
    if not args:
        return ""
    try:
        # Normalize common app name keys
        clean = {}
        for k, v in sorted(args.items()):
            if isinstance(v, str):
                v_clean = v.strip().lower()
                clean[k] = v_clean
            elif isinstance(v, (int, float, bool)):
                clean[k] = v
            elif isinstance(v, (list, tuple)):
                clean[k] = [str(item).strip().lower() if isinstance(item, str) else item for item in v]
            else:
                clean[k] = str(v)
        return json.dumps(clean, sort_keys=True)
    except Exception:
        return str(sorted(args.items()))


class ExecutionGuard:
    """Thread-safe tool execution guard and deduplication tracker."""

    def __init__(
        self,
        cooldown_seconds: float = 5.0,
        logger: Optional[Callable[[str], None]] = None,
        ui_logger: Optional[Callable[[str], None]] = None,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.logger = logger or print
        self.ui_logger = ui_logger or (lambda msg: None)

        self._lock = threading.RLock()
        self._current_request_id = 0
        self._last_request_time = time.monotonic()

        # Cache of executed tool call IDs: fc_id -> (timestamp, result)
        self._seen_tool_ids: dict[str, tuple[float, Any]] = {}

        # Per-user-request action map:
        # (request_id, tool_name, arg_fp) -> (timestamp, result)
        self._request_actions: dict[tuple[int, str, str], tuple[float, Any]] = {}

        # Recent action map (across requests for short cooldown):
        # (tool_name, arg_fp) -> (timestamp, result)
        self._recent_actions: dict[tuple[str, str], tuple[float, Any]] = {}

        # Currently in-progress actions: set of (tool_name, arg_fp)
        self._in_progress: set[tuple[str, str]] = set()

    @property
    def current_request_id(self) -> int:
        with self._lock:
            return self._current_request_id

    def new_user_request(self, source: str = "speech") -> int:
        """Advance the user command context when a new user turn or command begins."""
        with self._lock:
            self._current_request_id += 1
            self._last_request_time = time.monotonic()
            req_id = self._current_request_id

            # Periodic cleanup of old entries (older than 10 minutes)
            now = self._last_request_time
            if len(self._seen_tool_ids) > 100:
                self._seen_tool_ids = {
                    k: v for k, v in self._seen_tool_ids.items() if (now - v[0]) < 600
                }
            if len(self._request_actions) > 100:
                self._request_actions = {
                    k: v for k, v in self._request_actions.items() if (now - v[0]) < 600
                }
            if len(self._recent_actions) > 50:
                self._recent_actions = {
                    k: v for k, v in self._recent_actions.items() if (now - v[0]) < 60
                }

        self.logger(f"[tars] [GUARD] User Request #{req_id} started ({source})")
        return req_id

    def check_duplicate(
        self, fc_id: Optional[str], tool_name: str, args: dict
    ) -> tuple[bool, str, Any]:
        """Check whether this tool call is a duplicate.
        Returns (is_duplicate, reason_token, previous_or_cached_result).
        """
        # Alias normalization
        norm_name = "open_app" if tool_name == "open_application" else tool_name
        arg_fp = _fingerprint_args(args)
        now = time.monotonic()

        with self._lock:
            # 1. Exact Tool Call ID match
            if fc_id and fc_id in self._seen_tool_ids:
                _, cached_res = self._seen_tool_ids[fc_id]
                msg = f"TOOL: duplicate tool call ID '{fc_id}' ignored: {norm_name}"
                self.logger(f"[tars] [GUARD] {msg}")
                self.ui_logger(msg)
                return True, STATE_DUPLICATE_IGNORED, cached_res

            # 2. Check if this exact action is currently in-progress in another thread
            action_key = (norm_name, arg_fp)
            if action_key in self._in_progress:
                msg = f"TOOL: execution lock active — action in progress: {norm_name}({args})"
                self.logger(f"[tars] [GUARD] {msg}")
                self.ui_logger(msg)
                return (
                    True,
                    STATE_LAUNCH_IN_PROGRESS,
                    f"{STATE_LAUNCH_IN_PROGRESS}: '{norm_name}' is already executing.",
                )

            # 3. Check per-user-request action deduplication
            req_key = (self._current_request_id, norm_name, arg_fp)
            if req_key in self._request_actions:
                _, prev_res = self._request_actions[req_key]
                msg = f"TOOL: duplicate request ignored: {norm_name}({args})"
                self.logger(f"[tars] [GUARD] {msg} (Request #{self._current_request_id})")
                self.ui_logger(msg)
                return (
                    True,
                    STATE_DUPLICATE_IGNORED,
                    prev_res or f"{STATE_DUPLICATE_IGNORED}: Action already executed for this command.",
                )

            # 4. Check recent-action cooldown for stateful actions
            if norm_name in _STATEFUL_ACTIONS and action_key in self._recent_actions:
                last_time, prev_res = self._recent_actions[action_key]
                elapsed = now - last_time
                if elapsed < self.cooldown_seconds:
                    msg = f"TOOL: duplicate request ignored: {norm_name}({args})"
                    self.logger(
                        f"[tars] [GUARD] {msg} (Cooldown active: {elapsed:.1f}s < {self.cooldown_seconds}s)"
                    )
                    self.ui_logger(msg)
                    return (
                        True,
                        STATE_ALREADY_EXECUTED,
                        prev_res or f"{STATE_ALREADY_EXECUTED}: Action was executed {elapsed:.1f}s ago.",
                    )

            return False, "", None

    def record_start(self, tool_name: str, args: dict) -> None:
        """Mark an action as actively in-progress."""
        norm_name = "open_app" if tool_name == "open_application" else tool_name
        arg_fp = _fingerprint_args(args)
        with self._lock:
            self._in_progress.add((norm_name, arg_fp))

    def record_finish(
        self,
        fc_id: Optional[str],
        tool_name: str,
        args: dict,
        result: Any,
    ) -> None:
        """Record the authoritative execution result of a tool call."""
        norm_name = "open_app" if tool_name == "open_application" else tool_name
        arg_fp = _fingerprint_args(args)
        now = time.monotonic()

        with self._lock:
            action_key = (norm_name, arg_fp)
            self._in_progress.discard(action_key)

            if fc_id:
                self._seen_tool_ids[fc_id] = (now, result)

            req_key = (self._current_request_id, norm_name, arg_fp)
            self._request_actions[req_key] = (now, result)
            self._recent_actions[action_key] = (now, result)
