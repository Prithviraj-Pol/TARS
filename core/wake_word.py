"""
Local wake-word detection for TARS.

Design goals:
  • ZERO cost when the feature is off — openwakeword is imported ONLY inside
    start()/install helpers, never at module load. If the user never enables
    wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); the actual model inference runs in
    this module's own background thread, so the real-time audio thread and the
    Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; there is no
    network call except the one-time model download the user triggers from the UI.

The wake phrase is the single word "TARS", detected by a custom ONNX model
(models/tars.onnx) built using openwakeword's audio embedding backbone.
Generate the model with:  python create_tars_wake_model.py
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

WAKE_PHRASE = "TARS"
BASE_DIR = Path(__file__).resolve().parent.parent
CUSTOM_MODEL_PATH = BASE_DIR / "models" / "tars.onnx"

# Score in [0,1]; above this counts as a detection. Tunable per environment.
DEFAULT_THRESHOLD = 0.5
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000


def is_installed() -> bool:
    """True if the openwakeword package is importable (no model check)."""
    try:
        import importlib.util
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """True when the custom TARS model AND the openwakeword backbone are present.

    Checks for:
      1. models/tars.onnx          — custom TARS wake word ONNX model
      2. openwakeword/melspectrogram.onnx  — shared audio preprocessor
      3. openwakeword/embedding_model.onnx — shared audio embedding backbone

    This is a cheap, DETERMINISTIC file-existence check — never constructs
    a Model (that can clash with a running detector or be slow to import).
    Never raises.
    """
    if not is_installed():
        return False
    try:
        # 1. Custom TARS model
        if not CUSTOM_MODEL_PATH.exists():
            return False
        # 2 & 3. openwakeword backbone (ONNX only — tflite runtime not installed)
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        if not models_dir.is_dir():
            return False
        has_mel = any(models_dir.glob("melspectrogram*.onnx"))
        has_emb = any(models_dir.glob("embedding_model*.onnx"))
        return bool(has_mel and has_emb)
    except Exception:
        return False


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing, then
    download the backbone models (melspectrogram.onnx + embedding_model.onnx).
    Returns (ok, message). Never raises.

    Note: this downloads the openwakeword *backbone* only.  The custom TARS
    classifier (models/tars.onnx) is generated separately by:
        python create_tars_wake_model.py
    """
    _tell = notify or (lambda _msg: None)
    try:
        if not is_installed():
            logger("Wake word: installing openwakeword (one-time)...")
            _tell("Wake word: installing openwakeword (one-time)...")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"
        # Download the melspectrogram + embedding backbone models.
        # Passing [] downloads all feature/backbone models without re-downloading
        # any wake-word classifier (those come from create_tars_wake_model.py).
        logger("Wake word: downloading backbone models (melspectrogram + embedding)...")
        _tell("Wake word: downloading backbone models...")
        try:
            import openwakeword.utils as _u
            _u.download_models([])   # [] = feature models only (no wake classifiers)
        except Exception as e:
            return False, f"model download failed: {e}"

        # Check backbone is ready. Explicitly download ONNX feature models if missed.
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        has_mel = any(models_dir.glob("melspectrogram*.onnx"))
        has_emb = any(models_dir.glob("embedding_model*.onnx"))

        if not has_emb:
            try:
                _u.download_file("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx", str(models_dir))
                has_emb = any(models_dir.glob("embedding_model*.onnx"))
            except Exception:
                pass
        if not has_mel:
            try:
                _u.download_file("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx", str(models_dir))
                has_mel = any(models_dir.glob("melspectrogram*.onnx"))
            except Exception:
                pass

        if not (has_mel and has_emb):
            return False, "backbone download failed (melspectrogram.onnx or embedding_model.onnx missing)."

        if not CUSTOM_MODEL_PATH.exists():
            logger(f"Wake word: building custom '{WAKE_PHRASE}' model...")
            _tell(f"Wake word: building custom '{WAKE_PHRASE}' model...")
            try:
                import create_tars_wake_model
                create_tars_wake_model.main()
            except Exception as e:
                msg = (
                    f"Backbone ready. Run:  python create_tars_wake_model.py  "
                    f"to generate the custom '{WAKE_PHRASE}' wake word model ({e})."
                )
                logger(f"Wake word: {msg}")
                _tell(msg)
                return False, msg

        logger(f"Wake word: '{WAKE_PHRASE}' model ready.")
        return True, f"Wake word '{WAKE_PHRASE}' installed and ready."
    except Exception as e:
        return False, f"setup error: {e}"


class WakeWordDetector:
    """
    Runs the wake model in a dedicated thread. The mic thread calls feed() with
    raw int16 frames; detections invoke on_detect() (called from this thread —
    the callback must marshal to whatever loop/UI it needs).
    """

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = threshold
        self._logger    = logger
        # See PluginRegistry: `logger` is the console and gets everything,
        # `notify` is the activity log and gets only what the user must act on.
        self._notify    = notify or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._ready = False

    def start(self) -> bool:
        """Load the TARS model and spawn the inference thread. Returns True on success.
        Safe to call again — a no-op if already running. Never raises."""
        if self._running:
            return True
        if not CUSTOM_MODEL_PATH.exists():
            self._logger(
                f"Wake word model '{WAKE_PHRASE}' not found at {CUSTOM_MODEL_PATH}.  "
                f"Run:  python create_tars_wake_model.py"
            )
            self._notify(f"Wake word model '{WAKE_PHRASE}' unavailable — use the WAKE NOW button.")
            self._model = None
            return False
        try:
            from openwakeword.model import Model
            # Pass the absolute path — openwakeword loads custom ONNX files by path.
            self._model = Model(
                wakeword_models=[str(CUSTOM_MODEL_PATH)],
                inference_framework="onnx"
            )
        except Exception as e:
            self._logger(f"Wake word: could not load model — {e}")
            self._notify(f"Wake word model '{WAKE_PHRASE}' unavailable — use the WAKE NOW button.")
            self._model = None
            return False
        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger(f"Wake word: listening for '{WAKE_PHRASE}'.")
        return True

    def stop(self) -> None:
        self._running = False
        # unblock the thread if it's waiting on the queue
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        self._model = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap and
        never block — the frame is copied and dropped if the queue is backed up."""
        if not self._running:
            return
        try:
            # frame_int16 is a numpy int16 array (possibly 2-D mono) — flatten to 1-D
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _loop(self) -> None:
        import numpy as np
        import time
        last_detect = 0.0
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                scores = self._model.predict(np.asarray(frame, dtype=np.int16))
                score = 0.0
                if isinstance(scores, dict):
                    # Match the TARS model key; fall back to max of all scores
                    # so a key rename never silently breaks detection.
                    for k, v in scores.items():
                        if "tars" in k.lower():
                            score = max(score, float(v))
                    if score == 0.0 and scores:
                        score = max(float(v) for v in scores.values())
                now = time.monotonic()
                if score >= self._threshold and (now - last_detect) > 1.5:
                    last_detect = now
                    # drain any backlog so we don't double-fire on the same utterance
                    self._drain()
                    try:
                        self._on_detect()
                    except Exception as e:
                        self._logger(f"Wake word: on_detect error — {e}")
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass
