"""MLX Whisper adapter. All model calls use a preinstalled local directory."""
from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path

from .timeline import asr_windows, segment_to_utterance


def _local_model(config):
    value = config.get("asr_model_path")
    if not value:
        raise ValueError("asr_model_path is required; install local models first")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir() or not (path / "config.json").is_file():
        raise ValueError("asr_model_path must be a local MLX model directory")
    if not any((path / name).is_file() for name in ("weights.safetensors", "weights.npz")):
        raise ValueError("local ASR weights are missing")
    return path


@functools.lru_cache(maxsize=4)
def _fingerprint(path, size, modified_ns):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_model_identity(config):
    path = _local_model(config)
    weights = path / "weights.safetensors"
    if not weights.is_file():
        weights = path / "weights.npz"
    state = weights.stat()
    digest = _fingerprint(str(weights), state.st_size, state.st_mtime_ns)
    identity = {"asr_weights_sha256": digest,
                "asr_config_sha256": hashlib.sha256((path / "config.json").read_bytes()).hexdigest()}
    if digest == "951ed3fc1203e6a62467abb2144a96ce7eafca8fa77e3704fdb8635ff3e7f8a6":
        identity.update({"asr_repository": "mlx-community/whisper-large-v3-turbo",
                         "asr_revision": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"})
    return identity


def transcribe_regions(pcm, regions, metadata, config):
    """Return utterances and metrics; pcm is a mono float32 16 kHz memmap.

    No saved transcript is printed. Model failures propagate to the caller.
    Silence is decided by the acoustic stage, never from empty ASR text.
    """
    model_path = _local_model(config)
    # Defense in depth: local paths plus offline Hub mode; no credential access.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import numpy as np
    import mlx.core as mx
    import mlx_whisper

    windows = list(asr_windows(regions, int(config.get("asr_window_ms", 30000)),
                               int(config.get("asr_context_ms", 1000))))
    utterances = []
    input_ms = empty_windows = discarded_context = duplicates = 0
    for window in windows:
        left, right = window["start_ms"], window["end_ms"]
        samples = np.array(pcm[left * 16:right * 16], dtype=np.float32, copy=True)
        if not len(samples):
            raise ValueError("empty ASR window")
        input_ms += right - left
        result = mlx_whisper.transcribe(
            samples, path_or_hf_repo=str(model_path), language=config.get("language", "zh"),
            word_timestamps=True, verbose=None, temperature=0.0,
            condition_on_previous_text=False, fp16=True,
        )
        segments = result.get("segments")
        if not isinstance(segments, list):
            raise ValueError("ASR response has no segment list")
        mapping = [{"derived_start_ms": 0, "derived_end_ms": right - left,
                    "chunk_id": metadata["chunk_id"], "sha256": metadata["sha256"],
                    "start_ms": left, "end_ms": right}]
        emitted = 0
        for segment in segments:
            utterance = segment_to_utterance(segment, mapping, right - left)
            if utterance is None:
                continue
            span = utterance["source_spans"][0]
            midpoint = (span["start_ms"] + span["end_ms"]) / 2
            if not window["core_start_ms"] <= midpoint < window["core_end_ms"]:
                discarded_context += 1
                continue
            # Deduplicate only equal text AND intersecting source time; repeated
            # short replies at different times are intentionally preserved.
            duplicate = any(
                old["text"] == utterance["text"]
                and old["source_spans"][0]["start_ms"] < span["end_ms"]
                and span["start_ms"] < old["source_spans"][0]["end_ms"]
                for old in utterances[-12:]
            )
            if duplicate:
                duplicates += 1
                continue
            utterances.append(utterance)
            emitted += 1
        empty_windows += emitted == 0
        del samples, result
        mx.clear_cache()
    utterances.sort(key=lambda u: (u["source_spans"][0]["start_ms"], u["id"]))
    return utterances, {"asr_windows": len(windows), "asr_input_ms": input_ms,
                        "asr_empty_windows": empty_windows,
                        "asr_context_segments_discarded": discarded_context,
                        "asr_duplicate_segments": duplicates,
                        "mlx_peak_memory_bytes": int(mx.get_peak_memory())}
