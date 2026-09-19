"""Pure, sample-derived timeline operations; no audio/model imports."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata


def merge_intervals(intervals, gap_ms=0):
    """Union half-open intervals, optionally preserving short intervening gaps."""
    if gap_ms < 0:
        raise ValueError("gap_ms must be nonnegative")
    merged = []
    for start, end in sorted(intervals):
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end):
            raise ValueError("invalid interval")
        if merged and start <= merged[-1][1] + gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def map_source_spans(start_ms, end_ms, mapping):
    """Map a derived-media interval to original spans, omitting synthetic gaps.

    Each map entry has derived_start_ms/derived_end_ms and either chunk_id,
    sha256, start_ms/end_ms or chunk_id=None (explicit artificial silence).
    Real entries must have identical source and derived lengths: no time stretch.
    """
    if not (isinstance(start_ms, int) and isinstance(end_ms, int)
            and 0 <= start_ms < end_ms):
        raise ValueError("invalid derived interval")
    result, previous_end = [], 0
    for entry in mapping:
        a, b = entry["derived_start_ms"], entry["derived_end_ms"]
        if not (isinstance(a, int) and isinstance(b, int) and previous_end <= a < b):
            raise ValueError("invalid or overlapping mapping")
        previous_end = b
        if entry.get("chunk_id") is None:
            continue
        source_a, source_b = entry["start_ms"], entry["end_ms"]
        if not (isinstance(source_a, int) and isinstance(source_b, int)
                and 0 <= source_a < source_b and source_b - source_a == b - a):
            raise ValueError("source and derived lengths must match")
        if not entry.get("sha256"):
            raise ValueError("source hash required")
        left, right = max(a, start_ms), min(b, end_ms)
        if left >= right:
            continue
        span = {"chunk_id": entry["chunk_id"], "sha256": entry["sha256"],
                "start_ms": source_a + left - a, "end_ms": source_a + right - a}
        if (result and all(result[-1][k] == span[k] for k in ("chunk_id", "sha256"))
                and result[-1]["end_ms"] == span["start_ms"]):
            result[-1]["end_ms"] = span["end_ms"]
        else:
            result.append(span)
    return result


def stable_utterance_id(text, source_spans):
    payload = {"text": unicodedata.normalize("NFC", text).strip(), "source_spans": source_spans}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    return "utt_" + digest[:32]


def segment_to_utterance(segment, mapping, derived_duration_ms):
    """Use model segment times, never estimate time from character/word count."""
    text = segment.get("text", "")
    if not isinstance(text, str):
        raise ValueError("model segment text must be a string")
    text = text.strip()
    if not text:
        return None
    start, end = float(segment["start"]), float(segment["end"])
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError("invalid model segment timestamps")
    # Clip encoder padding at the actual media boundary, without inventing times.
    left = max(0, round(start * 1000))
    right = min(derived_duration_ms, round(end * 1000))
    if left >= right:
        raise ValueError("model segment lies outside media")
    spans = map_source_spans(left, right, mapping)
    if not spans:
        raise ValueError("model segment has no original audio source")
    return {"id": stable_utterance_id(text, spans), "text": text,
            "source_spans": spans, "timing_quality": "model_segment"}


def asr_windows(regions, max_window_ms=30000, context_ms=1000):
    """Bounded overlapping windows with exclusive midpoint ownership cores.

    Only silence/non_speech spans may be omitted. Context never crosses a skipped
    region. Ownership makes one segment belong to one window; source timestamps
    remain the full model interval, rather than clipping text to the core.
    """
    if not (4000 <= max_window_ms <= 120000 and 0 <= context_ms < max_window_ms / 4):
        raise ValueError("invalid ASR window size")
    retained = merge_intervals([(r["start_ms"], r["end_ms"]) for r in regions
                                if r["kind"] in ("speech", "uncertain")])
    core_size = max_window_ms - 2 * context_ms
    for left, right in retained:
        core_left = left
        while core_left < right:
            core_right = min(right, core_left + core_size)
            yield {"start_ms": max(left, core_left - context_ms),
                   "end_ms": min(right, core_right + context_ms),
                   "core_start_ms": core_left, "core_end_ms": core_right}
            core_left = core_right
