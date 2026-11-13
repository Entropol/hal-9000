"""Turkish in HAL's voice: kNN-VC voice conversion.

A neutral Turkish voice reads the text (correct pronunciation and rhythm), then kNN-VC swaps
its timbre for HAL's. The reference is HAL's own Piper voice reading `ref_sentences.txt`:
clean, consistent speech in exactly the voice the English replies use. (In tests it matched the
English HAL voice better than the 96 noisy movie clips did.)

`python -m app.vc` runs at image build time: it downloads the Piper Turkish voice (fallback
source) and the kNN-VC models, and precomputes HAL's matching set.
"""
import logging
import os
import shutil
import threading

import numpy as np
import torch
import torchaudio

log = logging.getLogger("hal.vc")

MATCHING_PATH = os.environ.get("HAL_MATCHING_SET", "/models/hal_matching_set.pt")
HAL_MODEL = os.environ.get("PIPER_MODEL", "/models/hal.onnx")
TR_DIR = os.environ.get("TR_VOICE_DIR", "/models/tr")
TR_VOICE_FILE = "tr_TR-dfki-medium.onnx"
SENTENCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref_sentences.txt")
TOPK = int(os.environ.get("VC_TOPK", "8"))  # higher = smoother, lower = closer to the reference
SR = 16000
# Semitones to raise (+) or lower (-) the Turkish source voice before it becomes HAL.
# 0 = off. Try -2, -3 for a deeper voice or +2 for a higher one; 12 semitones = one octave.
PITCH_SHIFT = max(-12.0, min(12.0, float(os.environ.get("TR_PITCH_SHIFT", "0") or 0)))
# Bass boost on the Turkish output: a low shelf of BASS_DB decibels below BASS_FREQ Hz.
# 0 = off. 4-8 is a clear, natural boost; much more starts to sound boomy.
BASS_DB = max(0.0, min(15.0, float(os.environ.get("TR_BASS_DB", "6") or 0)))
BASS_FREQ = float(os.environ.get("TR_BASS_FREQ", "200") or 200)


def _load_knn():
    return torch.hub.load("bshall/knn-vc", "knn_vc", prematched=True,
                          trust_repo=True, pretrained=True, device="cpu")


def _retry(fn, attempts: int = 5):
    """Build-time downloads occasionally drop mid-transfer; try again."""
    import time
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1:
                raise
            print(f"download failed ({exc!r}), retrying {i + 2}/{attempts}", flush=True)
            time.sleep(5 * (i + 1))


class Converter:
    def __init__(self) -> None:
        self.knn = _load_knn()
        self.matching = torch.load(MATCHING_PATH, weights_only=True)
        self.lock = threading.Lock()  # one conversion at a time
        log.info("kNN-VC ready, HAL matching set %s, topk=%s, pitch shift %+.1f st, "
                 "bass +%.1f dB below %.0f Hz",
                 tuple(self.matching.shape), TOPK, PITCH_SHIFT, BASS_DB, BASS_FREQ)

    def convert(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        """int16 mono PCM in -> int16 mono PCM at 16 kHz in HAL's timbre."""
        x = torch.from_numpy(pcm.astype(np.float32) / 32768.0)[None]
        x = torchaudio.functional.resample(x, sample_rate, SR)
        if PITCH_SHIFT:
            try:  # shift the source before matching; the output pitch follows it
                x = torchaudio.functional.pitch_shift(x, SR, PITCH_SHIFT)
            except Exception:
                log.exception("Pitch shift failed, using the unshifted voice")
        with self.lock, torch.inference_mode():
            query = self.knn.get_features(x)
            out = self.knn.match(query, self.matching, topk=TOPK)
        if BASS_DB:
            try:  # low-shelf boost: a deeper, fuller voice
                out = torchaudio.functional.bass_biquad(out.cpu(), SR, BASS_DB, BASS_FREQ)
            except Exception:
                log.exception("Bass boost failed, using the unboosted voice")
        out = out.cpu().numpy()
        out = out / max(float(np.abs(out).max()), 1e-6) * 0.9
        return (out * 32767).astype(np.int16)


def build() -> None:
    """Build-time setup. Needs network access and the HAL Piper model already in place."""
    from huggingface_hub import hf_hub_download
    from piper import PiperVoice

    os.makedirs(TR_DIR, exist_ok=True)
    for name in (TR_VOICE_FILE, TR_VOICE_FILE + ".json"):
        path = _retry(lambda: hf_hub_download("rhasspy/piper-voices",
                                              f"tr/tr_TR/dfki/medium/{name}"))
        shutil.copy(path, os.path.join(TR_DIR, name))

    knn = _retry(_load_knn)
    hal = PiperVoice.load(HAL_MODEL, config_path=HAL_MODEL + ".json")
    sentences = [l.strip() for l in open(SENTENCES, encoding="utf-8") if l.strip()]

    feats, seconds = [], 0.0
    for text in sentences:
        pcm = np.frombuffer(b"".join(hal.synthesize_stream_raw(text)), dtype=np.int16)
        wav = torchaudio.functional.resample(
            torch.from_numpy(pcm.astype(np.float32) / 32768.0)[None], hal.config.sample_rate, SR)
        seconds += wav.shape[1] / SR
        try:  # trim silence when possible; the trimmer can fail on very short clips
            feats.append(knn.get_features(wav, weights=knn.weighting, vad_trigger_level=7))
        except Exception:
            feats.append(knn.get_features(wav, weights=knn.weighting, vad_trigger_level=0))
    matching = torch.cat(feats, dim=0).cpu()
    torch.save(matching, MATCHING_PATH)
    print(f"HAL matching set {tuple(matching.shape)} from {len(feats)} sentences "
          f"({seconds / 60:.1f} min of HAL speech)")


if __name__ == "__main__":
    build()
