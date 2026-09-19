"""Local, bounded audio analysis; originals are opened read-only and hash checked.

Silero ONNX's 16 kHz recurrent API follows its pinned MIT-licensed v6.2 wrapper.
This adapter deliberately does not import torch or any model at module import.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .timeline import merge_intervals

SAMPLE_RATE = 16000
PIPELINE_VERSION = "local-audio-v1"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def classify_frames(frames, duration_ms, config=None):
    """Partition all media into speech, conservative uncertain, or long silence.

    Frames have start_ms/end_ms, probability and rms_dbfs. No minimum speech
    length is imposed. Low VAD alone never proves noise is safe to remove.
    """
    config = config or {}
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ValueError("positive media duration required")
    padding = int(config.get("vad_padding_ms", 300))
    merge_gap = int(config.get("vad_merge_gap_ms", 800))
    min_silence = int(config.get("min_silence_ms", 8000))
    speech_threshold = float(config.get("vad_speech_threshold", 0.5))
    suspect_threshold = float(config.get("vad_suspect_threshold", 0.1))
    quiet_threshold = float(config.get("vad_quiet_threshold", 0.05))
    silence_dbfs = float(config.get("silence_dbfs", -50.0))
    if not (300 <= padding <= 2000 and 800 <= merge_gap <= 3000 and min_silence >= 8000
            and 0 <= quiet_threshold < suspect_threshold < speech_threshold <= 1
            and -100 <= silence_dbfs <= -45):
        raise ValueError("unsafe VAD thresholds")
    speech, suspect, quiet = [], [], []
    previous_end = 0
    for frame in frames:
        a, b, probability = frame["start_ms"], frame["end_ms"], float(frame["probability"])
        dbfs = float(frame["rms_dbfs"])
        if not (a == previous_end and a < b <= duration_ms
                and math.isfinite(probability) and 0 <= probability <= 1 and math.isfinite(dbfs)):
            raise ValueError("invalid or incomplete VAD frame timeline")
        previous_end = b
        padded = (max(0, a - padding), min(duration_ms, b + padding))
        if probability >= speech_threshold:
            speech.append(padded)
        if probability >= suspect_threshold:
            suspect.append(padded)
        if probability < quiet_threshold and dbfs <= silence_dbfs:
            quiet.append((a, b))
    if previous_end != duration_ms:
        raise ValueError("VAD frames must cover actual media duration")
    speech = merge_intervals(speech, merge_gap)
    protected = merge_intervals(suspect + [(a, b) for a, b in speech], merge_gap)
    # Subtract all padded possible voice before applying the >=8s skip threshold.
    long_quiet = []
    for a, b in merge_intervals(quiet):
        cursor = a
        for p, q in protected:
            if q <= cursor:
                continue
            if p >= b:
                break
            if p - cursor >= min_silence:
                long_quiet.append([cursor, p])
            cursor = max(cursor, q)
        if b - cursor >= min_silence:
            long_quiet.append([cursor, b])
    points = sorted({0, duration_ms} | {v for pair in speech + long_quiet for v in pair})
    regions = []
    speech_index = quiet_index = 0
    for left, right in zip(points, points[1:]):
        while speech_index < len(speech) and speech[speech_index][1] <= left:
            speech_index += 1
        while quiet_index < len(long_quiet) and long_quiet[quiet_index][1] <= left:
            quiet_index += 1
        if speech_index < len(speech) and speech[speech_index][0] <= left < speech[speech_index][1]:
            kind, reason = "speech", "silero_speech_with_padding_and_short_pause"
        elif quiet_index < len(long_quiet) and long_quiet[quiet_index][0] <= left < long_quiet[quiet_index][1]:
            kind, reason = "silence", "continuous_low_vad_and_low_energy_at_least_8s"
        else:
            kind, reason = "uncertain", "retained_possible_voice_noise_or_short_pause"
        if regions and regions[-1]["kind"] == kind:
            regions[-1]["end_ms"] = right
        else:
            regions.append({"start_ms": left, "end_ms": right, "kind": kind, "reason": reason})
    return regions


def _vad_frames(pcm, model_path):
    import numpy as np
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    session = ort.InferenceSession(str(model_path), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, 64), dtype=np.float32)
    frames = []
    clipped = 0
    square_sum = 0.0
    # Duration is truncated to full milliseconds before inference.
    duration_ms = len(pcm) // 16
    for offset in range(0, len(pcm), 512):
        raw = np.asarray(pcm[offset:offset + 512])
        clipped += int(np.count_nonzero(np.abs(raw) >= 0.999))
        energy = float(np.sum(np.square(raw, dtype=np.float64)))
        square_sum += energy
        dbfs = 10 * math.log10(max(energy / len(raw), 1e-12))
        block = np.zeros((1, 512), dtype=np.float32)
        block[0, :len(raw)] = raw
        model_input = np.concatenate([context, block], axis=1)
        probability, state = session.run(None, {"input": model_input, "state": state,
                                                "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
        context = model_input[:, -64:]
        frames.append({"start_ms": offset // 16,
                       "end_ms": min(duration_ms, (offset + len(raw)) // 16),
                       "probability": float(probability.reshape(-1)[0]), "rms_dbfs": dbfs})
    return frames, {"vad_frames": len(frames), "clipped_sample_ratio": clipped / len(pcm),
                    "rms_dbfs": 10 * math.log10(max(square_sum / len(pcm), 1e-12))}


def _tools(config):
    ffmpeg = str(config.get("ffmpeg_path") or shutil.which("ffmpeg") or "")
    ffprobe = str(config.get("ffprobe_path") or (str(Path(ffmpeg).with_name("ffprobe")) if ffmpeg else ""))
    if not ffmpeg or not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        raise FileNotFoundError("FFmpeg and ffprobe are required; configure absolute ffmpeg_path")
    return ffmpeg, ffprobe


def _decode_pcm(path, output, ffmpeg, seconds):
    # Keep container PTS: AAC skip-samples/edit lists can put the first decoded
    # sample after zero. first_pts=0 fills that media gap instead of shifting all
    # speech earlier; async=1 also respects timestamp gaps inside the stream.
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-copyts", "-i", str(path),
                    "-map", "0:a:0", "-vn", "-ac", "1",
                    "-af", "aresample=16000:async=1:first_pts=0",
                    "-t", str(seconds), "-f", "f32le", "-acodec", "pcm_f32le", str(output)],
                   check=True, capture_output=True, timeout=300)
    return output.stat().st_size // 4


def analyze_chunk(audio_path: Path, metadata: dict, config: dict) -> dict:
    """Analyze one immutable chunk; raise on corrupt audio/model/config/ASR.

    Return duration_ms, complete regions, stable utterances, metrics, model.
    All source offsets are half-open original playback media milliseconds.
    No cloud fallback and no timestamp inference from text are permitted.
    """
    started = time.monotonic()
    path = Path(audio_path).expanduser().resolve(strict=True)
    for field in ("chunk_id", "sha256", "duration_seconds", "started_at", "session_id", "sequence"):
        if field not in metadata:
            raise ValueError("missing metadata field: " + field)
    original_sha = _sha256(path)
    if original_sha != metadata["sha256"]:
        raise ValueError("original audio SHA256 does not match metadata")
    vad_value = config.get("vad_model_path")
    if not vad_value:
        raise ValueError("vad_model_path is required; install local models first")
    vad_path = Path(vad_value).expanduser().resolve(strict=True)
    if not vad_path.is_file():
        raise ValueError("VAD model is not a local file")
    ffmpeg, ffprobe = _tools(config)
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=duration,start_time:format=duration", "-of", "json", str(path)],
                           check=True, capture_output=True, timeout=30)
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise ValueError("media has no audio stream")
    seconds = float(info.get("format", {}).get("duration", streams[0].get("duration", "nan")))
    max_seconds = float(config.get("max_chunk_seconds", 900))
    if not (math.isfinite(seconds) and 0 < seconds <= max_seconds <= 1800):
        raise ValueError("media duration unavailable or exceeds bounded chunk limit")
    declared_seconds = float(metadata["duration_seconds"])
    if not math.isfinite(declared_seconds) or declared_seconds <= 0:
        raise ValueError("invalid metadata duration")
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="hemory-semantic-") as temp:
        pcm_path = Path(temp) / "mono-16k.f32"
        sample_count = _decode_pcm(path, pcm_path, ffmpeg, seconds)
        # Ignore sub-ms tail and any codec pad beyond the declared media end.
        duration_ms = min(sample_count // 16, math.floor(seconds * 1000))
        if duration_ms <= 0:
            raise ValueError("decoded media is empty")
        if seconds - sample_count / SAMPLE_RATE > 0.25:
            raise ValueError("decoded audio is unexpectedly shorter than media duration")
        pcm = np.memmap(pcm_path, dtype="<f4", mode="r", shape=(duration_ms * 16,))
        if not np.isfinite(pcm).all():
            raise ValueError("decoded audio contains non-finite samples")
        frames, metrics = _vad_frames(pcm, vad_path)
        regions = classify_frames(frames, duration_ms, config)
        vad_seconds = time.monotonic() - started
        retained_ms = sum(r["end_ms"] - r["start_ms"] for r in regions if r["kind"] in ("speech", "uncertain"))
        if retained_ms:
            from .transcribe import local_model_identity, transcribe_regions
            asr_identity = local_model_identity(config)
            utterances, asr_metrics = transcribe_regions(pcm, regions, metadata, config)
        else:
            asr_identity = {}
            utterances, asr_metrics = [], {"asr_windows": 0, "asr_input_ms": 0, "asr_empty_windows": 0}
        del pcm
    # Detect source mutation during analysis; do not return a valid-looking run.
    if _sha256(path) != original_sha:
        raise ValueError("original audio changed during analysis")
    elapsed = time.monotonic() - started
    metrics.update(asr_metrics)
    metrics.update({"retained_ms": retained_ms, "skipped_ms": duration_ms - retained_ms,
                    "speech_ms": sum(r["end_ms"] - r["start_ms"] for r in regions if r["kind"] == "speech"),
                    "uncertain_ms": sum(r["end_ms"] - r["start_ms"] for r in regions if r["kind"] == "uncertain"),
                    "metadata_duration_delta_ms": duration_ms - round(declared_seconds * 1000),
                    "decoded_duration_ms": sample_count / 16,
                    "media_start_time_seconds": float(streams[0].get("start_time", 0)),
                    "vad_and_decode_seconds": round(vad_seconds, 3),
                    "elapsed_seconds": round(elapsed, 3), "real_time_factor": round(elapsed / (duration_ms / 1000), 4),
                    "utterance_count": len(utterances),
                    "vad_probability_max": max(f["probability"] for f in frames)})
    model = {"pipeline": PIPELINE_VERSION, "vad": "silero-onnx",
             "vad_sha256": _sha256(vad_path), "asr": "mlx-whisper-local",
             "asr_model_path": str(config.get("asr_model_path", "")),
             "language": config.get("language", "zh"), "word_timestamps": True,
             "sample_rate": SAMPLE_RATE, "offline": True,
             "onnxruntime_version": importlib.metadata.version("onnxruntime")}
    model.update(asr_identity)
    if model["vad_sha256"] == "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3":
        model["vad_revision"] = "be95df9152c0d7618fa1edfeb296fc3dae32376f"
    if retained_ms:
        model["mlx_whisper_version"] = importlib.metadata.version("mlx-whisper")
    return {"duration_ms": duration_ms, "regions": regions, "utterances": utterances,
            "metrics": metrics, "model": model}
