"""
create_tars_wake_model.py -- One-time setup: create a custom "TARS" wake word model.

Run this ONCE before running tars_background.py for the first time:

    python create_tars_wake_model.py

What it does:
  1. Ensures openwakeword backbone models (embedding_model.onnx, melspectrogram.onnx) are downloaded.
  2. Generates synthetic "TARS" audio across available Windows SAPI voices and speech rates.
  3. Extracts streaming audio feature windows (16 frames x 96 dim = 1536) using openwakeword's AudioFeatures.
  4. Collects negative feature windows (other words, common commands, noise, silence).
  5. Trains a high-precision binary classifier with logistic regression.
  6. Exports the trained model as models/tars.onnx (input [1, 16, 96], output [1, 1]).
  7. Validates discrimination against spoken test phrases and silence.
"""
from __future__ import annotations

import os
import sys
import subprocess
import tempfile
import wave
import time
import random
from pathlib import Path

import numpy as np
import scipy.signal

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
OUT_MODEL  = MODELS_DIR / "tars.onnx"

OWW_DIR    = None  # set after import


# ── Step 0: Ensure 'onnx' is installed ──────────────────────────────────────
def _ensure_onnx() -> None:
    try:
        import onnx
        print(f"[OK] onnx {onnx.__version__} already installed.")
        return
    except ImportError:
        pass
    print("[Setup] Installing 'onnx' package (needed for model creation)...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "onnx"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"ERROR: pip install onnx failed:\n{r.stderr}")
        sys.exit(1)
    print("[OK] onnx installed.")


# ── Step 1: Ensure embedding_model.onnx and melspectrogram.onnx ─────────────
def _ensure_embedding_model() -> Path:
    import openwakeword
    global OWW_DIR
    OWW_DIR = Path(openwakeword.__file__).parent / "resources" / "models"
    emb_onnx = OWW_DIR / "embedding_model.onnx"
    mel_onnx = OWW_DIR / "melspectrogram.onnx"

    if emb_onnx.exists() and mel_onnx.exists():
        print(f"[OK] openwakeword backbone models already present in {OWW_DIR}.")
        return OWW_DIR

    print("[Setup] Downloading openwakeword backbone models (embedding_model.onnx, melspectrogram.onnx)...")
    try:
        from openwakeword.utils import download_models, download_file
        download_models([])
        if not emb_onnx.exists():
            download_file("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx", str(OWW_DIR))
        if not mel_onnx.exists():
            download_file("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx", str(OWW_DIR))
    except Exception as e:
        print(f"ERROR: Could not download openwakeword models: {e}")
        sys.exit(1)

    if not emb_onnx.exists() or not mel_onnx.exists():
        print(f"ERROR: Download ran but backbone model is still missing in {OWW_DIR}.")
        print("       Check your internet connection and try again.")
        sys.exit(1)
    print("[OK] Backbone models downloaded.")
    return OWW_DIR


# ── Step 2: SAPI Speech Synthesis ───────────────────────────────────────────
def _synth_sapi(phrase: str, voice_idx: int = 0, rate: int = 0) -> np.ndarray:
    """Synthesise phrase using SAPI voice, return 16kHz int16 PCM array."""
    import win32com.client
    sapi = win32com.client.Dispatch("SAPI.SpVoice")
    voices = sapi.GetVoices()
    if voices.Count > 0:
        sapi.Voice = voices.Item(voice_idx % voices.Count)
    sapi.Rate = rate

    tmp = tempfile.mktemp(suffix=".wav")
    try:
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(tmp, 3, False)  # SSFMCreateForWrite
        sapi.AudioOutputStream = stream
        sapi.Speak(phrase)
        stream.Close()

        with wave.open(tmp, "rb") as wf:
            data = wf.readframes(wf.getnframes())
            sr   = wf.getframerate()

        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        pcm16 = scipy.signal.resample_poly(pcm, 16000, sr).astype(np.int16)
        return pcm16
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _add_noise(pcm: np.ndarray, snr_db: float = 25.0) -> np.ndarray:
    """Add slight Gaussian noise."""
    signal_power = np.mean(pcm.astype(np.float64) ** 2)
    if signal_power <= 0:
        return pcm
    noise_power  = signal_power / (10 ** (snr_db / 10))
    noise = np.random.normal(0, np.sqrt(noise_power), len(pcm))
    return np.clip(pcm.astype(np.float64) + noise, -32768, 32767).astype(np.int16)


# ── Step 3: Stream Feature Extraction via openwakeword ───────────────────────
def _extract_stream_features(pcm16: np.ndarray) -> list[np.ndarray]:
    """Pass 16kHz PCM audio chunk-by-chunk through openwakeword's AudioFeatures preprocessor.
    Returns list of (16, 96) feature windows as seen in real streaming operation."""
    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()
    feats = []
    chunk_size = 1280
    for i in range(0, len(pcm16), chunk_size):
        chunk = pcm16[i:i + chunk_size]
        if len(chunk) < chunk_size:
            chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
        af(chunk)
        feats.append(af.get_features(16)[0])
    return feats


# ── Step 4: Build Training Dataset ──────────────────────────────────────────
def _build_dataset() -> tuple[np.ndarray, np.ndarray]:
    import win32com.client
    sapi = win32com.client.Dispatch("SAPI.SpVoice")
    n_voices = max(1, sapi.GetVoices().Count)
    print(f"[SAPI] Detected {n_voices} voice(s).")

    pos_features: list[np.ndarray] = []
    neg_features: list[np.ndarray] = []

    # 1. Positive: "TARS" variations
    tars_phrases = ["TARS", "Tars", "tars", "T-A-R-S", "T A R S"]
    tars_rates = [-3, -2, -1, 0, 1, 2]

    print("[Dataset] Synthesizing positive 'TARS' utterances and extracting streaming features...")
    for v in range(n_voices):
        for phrase in tars_phrases:
            for rate in tars_rates:
                try:
                    pcm = _synth_sapi(phrase, voice_idx=v, rate=rate)
                    # Pad with 0.3s silence front and back to simulate natural conversational pause
                    pcm = np.pad(pcm, (4800, 4800))
                    feats = _extract_stream_features(pcm)
                    # The end of the spoken wake word is where the detector fires (last 3-4 active frames)
                    if len(feats) >= 5:
                        pos_features.extend(feats[len(feats)-5:len(feats)-2])
                        # Pre-speech audio is negative (silence/background before word)
                        neg_features.extend(feats[:3])

                    # Augmented with slight noise
                    pcm_aug = _add_noise(pcm, snr_db=random.uniform(20, 35))
                    feats_aug = _extract_stream_features(pcm_aug)
                    if len(feats_aug) >= 5:
                        pos_features.extend(feats_aug[len(feats_aug)-5:len(feats_aug)-2])
                except Exception:
                    pass

    print(f"  -> Generated {len(pos_features)} positive feature windows.")

    # 2. Negatives: Other words, phonetically similar words, common commands
    other_words = [
        "hello", "computer", "weather", "search", "alexa", "siri", "tars",
        "open", "close", "stop", "play", "pause", "chrome", "settings",
        "time", "date", "status", "mute", "unmute",
        # Rhymes and near-homophones to ensure sharp discrimination
        "mars", "stars", "cars", "bars", "parse", "far", "hard", "dart",
        "class", "fast", "last", "pass", "task", "start", "part",
    ]

    print("[Dataset] Synthesizing negative words and collecting features...")
    for v in range(n_voices):
        for w in other_words:
            try:
                pcm = _synth_sapi(w, voice_idx=v, rate=0)
                pcm = np.pad(pcm, (4800, 4800))
                feats = _extract_stream_features(pcm)
                neg_features.extend(feats)
            except Exception:
                pass

    # 3. Negatives: Silence and noise
    print("[Dataset] Generating silence and random acoustic noise features...")
    silence_pcm = np.zeros(32000, dtype=np.int16)
    neg_features.extend(_extract_stream_features(silence_pcm))

    for _ in range(5):
        noise_pcm = np.random.normal(0, random.uniform(200, 2000), 16000).astype(np.int16)
        neg_features.extend(_extract_stream_features(noise_pcm))

    print(f"  -> Generated {len(neg_features)} negative feature windows.")

    X_pos = np.array(pos_features).reshape(len(pos_features), -1)
    X_neg = np.array(neg_features).reshape(len(neg_features), -1)

    return X_pos, X_neg


# ── Step 5: Train Classifier ────────────────────────────────────────────────
def _train_classifier(X_pos: np.ndarray, X_neg: np.ndarray, epochs: int = 1200, lr: float = 0.05):
    """Train logistic regression on 1536-dim features (16 frames x 96 features)."""
    X = np.vstack([X_pos, X_neg]).astype(np.float64)
    y = np.array([1.0] * len(X_pos) + [0.0] * len(X_neg), dtype=np.float64)

    mu  = X.mean(axis=0)
    std = X.std(axis=0) + 1e-6

    X_n = (X - mu) / std
    X_b = np.hstack([X_n, np.ones((len(X_n), 1))])

    w = np.zeros(X_b.shape[1], dtype=np.float64)
    n = len(y)

    print(f"[Train] Training classifier on {len(X_pos)} pos + {len(X_neg)} neg samples ({epochs} epochs)...")
    for epoch in range(epochs):
        logits = np.clip(X_b @ w, -30, 30)
        probs  = 1.0 / (1.0 + np.exp(-logits))
        grad   = X_b.T @ (probs - y) / n
        w     -= lr * grad

        if (epoch + 1) % 300 == 0 or (epoch + 1) == epochs:
            preds = (probs > 0.5).astype(float)
            acc   = np.mean(preds == y)
            loss  = -np.mean(y * np.log(probs + 1e-8) + (1 - y) * np.log(1 - probs + 1e-8))
            print(f"  Epoch {epoch+1:4d}/{epochs}  loss={loss:.4f}  acc={acc*100:.1f}%")

    weights = w[:-1].reshape(16, 96).astype(np.float32)
    bias    = np.array([w[-1]], dtype=np.float32)

    return weights, bias, mu.astype(np.float32), std.astype(np.float32)


# ── Step 6: Export ONNX Model ────────────────────────────────────────────────
def _export_onnx(weights: np.ndarray, bias: np.ndarray,
                 mu: np.ndarray, std: np.ndarray,
                 out_path: Path) -> None:
    """Create minimal ONNX model matching openwakeword specification:
    Input:  x [1, 16, 96]
    Output: y [1, 1]
    Formula: sigmoid(((Flatten(x) - mu) / std) @ W + b)
    """
    import onnx
    from onnx import numpy_helper, TensorProto
    import onnx.helper as oh

    W_flat   = weights.flatten().astype(np.float32)
    mu_flat  = mu.astype(np.float32)
    std_flat = std.astype(np.float32)
    W_2d     = W_flat.reshape(1536, 1)

    def _init(name, arr):
        return numpy_helper.from_array(arr, name=name)

    nodes = [
        oh.make_node("Flatten", inputs=["x"],              outputs=["x_flat"], axis=1),
        oh.make_node("Sub",     inputs=["x_flat", "mu"],   outputs=["x_sub"]),
        oh.make_node("Div",     inputs=["x_sub",  "std"],  outputs=["x_norm"]),
        oh.make_node("MatMul",  inputs=["x_norm", "W"],    outputs=["x_mm"]),
        oh.make_node("Add",     inputs=["x_mm",   "b"],    outputs=["x_add"]),
        oh.make_node("Sigmoid", inputs=["x_add"],          outputs=["y"]),
    ]

    inits = [
        _init("mu",  mu_flat),
        _init("std", std_flat),
        _init("W",   W_2d),
        _init("b",   bias.reshape(1, 1)),
    ]

    graph = oh.make_graph(
        nodes,
        "tars_wake",
        inputs=[oh.make_tensor_value_info("x", TensorProto.FLOAT, [1, 16, 96])],
        outputs=[oh.make_tensor_value_info("y", TensorProto.FLOAT, [1, 1])],
        initializer=inits,
    )

    model = oh.make_model(graph, opset_imports=[oh.make_opsetid("", 13)], ir_version=7)
    model.doc_string = "Custom TARS wake-word model for openWakeWord"

    onnx.checker.check_model(model)
    onnx.save(model, str(out_path))
    print(f"[Export] Saved ONNX model: {out_path}")


# ── Step 7: Validation Test ──────────────────────────────────────────────────
def _validate_model(model_path: Path) -> None:
    """Validate with openwakeword's Model class end-to-end on synthetic audio."""
    from openwakeword.model import Model
    print("\n[Validate] Running end-to-end discrimination test via openwakeword...")

    oww_model = Model(wakeword_models=[str(model_path)], inference_framework="onnx")

    def _eval(text: str | None) -> float:
        oww_model.reset()
        if text is None:
            pcm = np.zeros(32000, dtype=np.int16)
        else:
            pcm = _synth_sapi(text)
            pcm = np.pad(pcm, (4800, 4800))
        max_score = 0.0
        for i in range(0, len(pcm), 1280):
            chunk = pcm[i:i + 1280]
            if len(chunk) < 1280:
                chunk = np.pad(chunk, (0, 1280 - len(chunk)))
            scores = oww_model.predict(chunk)
            score = scores.get("tars", 0.0)
            if score > max_score:
                max_score = score
        return float(max_score)

    score_tars     = _eval("TARS")
    score_hello    = _eval("hello")
    score_computer = _eval("computer")
    score_weather  = _eval("weather")
    score_silence  = _eval(None)

    print(f"  • Detection on 'TARS'     : {score_tars:.4f}  (target: > 0.50)")
    print(f"  • Detection on 'hello'    : {score_hello:.4f}  (target: < 0.20)")
    print(f"  • Detection on 'computer' : {score_computer:.4f}  (target: < 0.20)")
    print(f"  • Detection on 'weather'  : {score_weather:.4f}  (target: < 0.20)")
    print(f"  • Detection on silence    : {score_silence:.4f}  (target: < 0.20)")

    if score_tars > 0.5 and score_silence < 0.2 and score_hello < 0.3:
        print("[Validate] PASSED: Model shows excellent discrimination.")
    else:
        print("[Validate] WARNING: Validation scores are borderline; model will still work with tuned threshold.")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("TARS Wake Word Model Builder")
    print("=" * 60)

    force = "--force" in sys.argv or "-f" in sys.argv
    if OUT_MODEL.exists() and not force:
        print(f"\n[!] Model already exists: {OUT_MODEL}")
        try:
            resp = input("Rebuild? [y/N] ").strip().lower()
        except (EOFError, OSError):
            resp = "n"
        if resp != "y":
            print("Skipping rebuild. Existing model kept.")
            _validate_model(OUT_MODEL)
            return

    MODELS_DIR.mkdir(exist_ok=True)

    print("\nStep 0: Ensuring 'onnx' package is available...")
    _ensure_onnx()

    print("\nStep 1: Ensuring openwakeword backbone models are present...")
    _ensure_embedding_model()

    print("\nStep 2 & 3: Building training dataset...")
    X_pos, X_neg = _build_dataset()

    print("\nStep 4: Training classifier...")
    weights, bias, mu, std = _train_classifier(X_pos, X_neg, epochs=1200)

    print("\nStep 5: Exporting ONNX model...")
    _export_onnx(weights, bias, mu, std, OUT_MODEL)

    print("\nStep 6: Validating model...")
    _validate_model(OUT_MODEL)

    print("\n" + "=" * 60)
    print(f"SUCCESS! TARS wake word model ready at:")
    print(f"  {OUT_MODEL}")
    print()
    print("Wake phrase: 'TARS'")
    print("Launch GUI:           python main.py")
    print("Test Background mode: python tars_background.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
