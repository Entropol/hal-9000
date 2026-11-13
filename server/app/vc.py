"""Turkish in HAL's voice: kNN-VC voice conversion.

Piper's Turkish voice reads the text with correct pronunciation, then kNN-VC
swaps its timbre for HAL's, using the HAL dataset clips as the reference.

`python -m app.vc` runs at image build time: it downloads the Turkish voice and the
HAL reference clips, downloads the kNN-VC models, and precomputes HAL's matching set.
"""
import logging
import os
import shutil
import threading

import numpy as np
import soundfile as sf
import torch
import torchaudio

log = logging.getLogger("hal.vc")

MATCHING_PATH = os.environ.get("HAL_MATCHING_SET", "/models/hal_matching_set.pt")
TR_DIR = os.environ.get("TR_VOICE_DIR", "/models/tr")
TR_VOICE_FILE = "tr_TR-dfki-medium.onnx"
TOPK = int(os.environ.get("VC_TOPK", "4"))
SR = 16000


def _load_knn():
    return torch.hub.load("bshall/knn-vc", "knn_vc", prematched=True,
                          trust_repo=True, pretrained=True, device="cpu")


def _load_16k(path: str) -> torch.Tensor:
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = torch.from_numpy(x.mean(axis=1))[None]
    return torchaudio.functional.resample(x, sr, SR)


class Converter:
    def __init__(self) -> None:
        self.knn = _load_knn()
        self.matching = torch.load(MATCHING_PATH, weights_only=True)
        self.lock = threading.Lock()  # one conversion at a time
        log.info("kNN-VC ready, HAL matching set %s", tuple(self.matching.shape))

    def convert(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        """int16 mono PCM in -> int16 mono PCM at 16 kHz in HAL's timbre."""
        x = torch.from_numpy(pcm.astype(np.float32) / 32768.0)[None]
        x = torchaudio.functional.resample(x, sample_rate, SR)
        with self.lock, torch.inference_mode():
            query = self.knn.get_features(x)
            out = self.knn.match(query, self.matching, topk=TOPK)
        out = out.cpu().numpy()
        out = out / max(float(np.abs(out).max()), 1e-6) * 0.9
        return (out * 32767).astype(np.int16)


def build() -> None:
    """Build-time setup. Needs network access."""
    from huggingface_hub import hf_hub_download, snapshot_download

    os.makedirs(TR_DIR, exist_ok=True)
    for name in (TR_VOICE_FILE, TR_VOICE_FILE + ".json"):
        shutil.copy(hf_hub_download("rhasspy/piper-voices", f"tr/tr_TR/dfki/medium/{name}"),
                    os.path.join(TR_DIR, name))

    ref = snapshot_download("campwill/HAL-9000-Speech", repo_type="dataset",
                            allow_patterns=["audio/*"], local_dir="/tmp/hal_ref")
    ref_dir = os.path.join(ref, "audio")

    knn = _load_knn()
    feats, fallback = [], 0
    for name in sorted(os.listdir(ref_dir)):
        wav = _load_16k(os.path.join(ref_dir, name))
        try:  # trim silence when possible; some noisy clips break the trimmer
            feats.append(knn.get_features(wav, weights=knn.weighting, vad_trigger_level=7))
        except Exception:
            fallback += 1
            feats.append(knn.get_features(wav, weights=knn.weighting, vad_trigger_level=0))
    matching = torch.cat(feats, dim=0).cpu()
    torch.save(matching, MATCHING_PATH)
    print(f"HAL matching set {tuple(matching.shape)} from {len(feats)} clips "
          f"({fallback} without silence trimming)")
    shutil.rmtree("/tmp/hal_ref", ignore_errors=True)


if __name__ == "__main__":
    build()
