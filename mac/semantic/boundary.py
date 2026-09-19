"""Read-only, bounded cross-chunk acoustic context reconciliation.

Contract: reconcile_boundaries(sources, analyses, config, data_root) returns
{"utterances": [...], "replaced_ids": [...], "metrics": {...}}. Analyses accepts
{chunk_id: {state: "succeeded", result: analysis}} or direct analysis dictionaries.
Inputs are never mutated. No database, queue, credentials or cloud APIs are used.

Each returned utterance retains the timeline module's id/text/source_spans and
model_segment timing_quality, plus:
  boundary_status: "reconciled" | "context_candidate"
  review_required: bool
  replaces_ids: IDs eligible for replacement (empty for candidates)
  related_ids: intersecting original IDs (reference only; NOT deletion authority)
  boundary_id: deterministic pair ID
  epoch: source epoch + first source offset
  reason: machine-readable decision reason
Only "reconciled" entries belong in the main transcript. Keep context_candidate
entries in a separate review collection: appending them to ordinary utterances
would duplicate original text and mislead later summaries. Top-level replaced_ids
is the union of reconciled replaces_ids, never candidate related_ids.

Conservative eligibility: same session, adjacent sequence, declared and measured
media gaps within +/-250 ms, speech on BOTH sides within 250 ms of the seam,
successful analysis and duration calibration within 250 ms. Overlapping media
may produce candidates, but never automatic replacements. Each side is <=6 s.
The derived input explicitly represents positive media gaps as artificial silence;
map_source_spans omits these gaps, retaining original media millisecond offsets.

Replacement is intentionally rare: one cross-seam model segment must completely
contain every intersecting old utterance on both sides, match their concatenated
NFC text except redundant whitespace, closely cover the old endpoints (<=250 ms), and not
conflict with another candidate or an earlier boundary replacement. A changed
word, partial old sentence, missing transcript or ambiguous timing retains all
originals. Even accepted timings remain model estimates, not exact forced alignment.

Model/decode/validation failure is isolated to that pair, returns no replacement
and increments metrics.errors using exception TYPE only, never private text/path.
Empty ASR preserves originals. There is no durable cache; the caller should cache
by source hashes, analysis identities, config and this module's source hash.

Test injection (trusted in-process config, not JSON/user transcript instructions):
  _boundary_transcriber(pcm: array('f'), config: dict) -> {segments: [...]}
  _boundary_audio_loader(source, start_ms, end_ms, config, data_root) -> array('f')
The loader must return exactly (end_ms-start_ms)*16 finite float samples. Production
uses bounded FFmpeg input seeking with retained PTS and local MLX Whisper only.
Heavy packages are lazy imports. Use one sequential worker; MLX has process-global
model/cache state. No whole-chunk retranscription or persisted derived audio.

Validation: standard-library fixture tests include cross-file mapping, artificial
silence, discontinuities, failed/empty ASR and atomic pair failure. The real FFmpeg
regression uses offset AAC chirp audio; seeked samples agree with sequential decode
within one 16k sample (62.5us), below the 1ms mapping precision. This does not prove
speech-model alignment accuracy or claim lossless repair of changed words.
"""
from __future__ import annotations

from array import array
import hashlib
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid

from .timeline import segment_to_utterance


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _decode_window(source, start_ms, end_ms, config, data_root):
    chunk_id = source["chunk_id"]
    if str(uuid.UUID(chunk_id)) != chunk_id:
        raise ValueError("noncanonical source identity")
    root = Path(data_root).expanduser().resolve(strict=True)
    expected = root / "chunks" / chunk_id / (chunk_id + ".m4a")
    path = expected.resolve(strict=True)
    if path != expected or not path.is_file():
        raise ValueError("source path escapes canonical chunk location")
    if _digest(path) != source["sha256"]:
        raise ValueError("source hash mismatch")
    if not 0 <= start_ms < end_ms or end_ms - start_ms > 6000:
        raise ValueError("invalid bounded source window")
    executable = config.get("ffmpeg_path") or shutil.which("ffmpeg")
    if not executable:
        raise FileNotFoundError("FFmpeg is required")
    # Seek demux packets on the absolute container timeline. Disable FFmpeg's
    # accurate-seek trimming (which adds stream.start_time for some AAC inputs),
    # then subtract our requested absolute time BEFORE resampling. first_pts=0
    # trims packet preroll or fills a real leading gap; unlike nonzero first_pts,
    # this is independent of the input sample rate (e.g. 48k -> 16k).
    command = [str(executable), "-nostdin", "-v", "error", "-xerror", "-copyts",
               "-noaccurate_seek", "-seek_timestamp", "1", "-ss", f"{start_ms / 1000:.3f}", "-i", str(path),
               "-map", "0:a:0", "-vn", "-ac", "1", "-af",
               f"asetpts=PTS-({start_ms}/1000)/TB,aresample=16000:async=1:first_pts=0,atrim=end_sample={(end_ms-start_ms)*16},asetpts=PTS-STARTPTS",
               "-t", f"{(end_ms-start_ms)/1000:.3f}", "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    response = subprocess.run(command, check=True, capture_output=True, timeout=45)
    pcm = array("f")
    pcm.frombytes(response.stdout)
    if sys.byteorder != "little":
        pcm.byteswap()
    if len(pcm) != (end_ms - start_ms) * 16:
        raise ValueError("local decode cannot calibrate requested source range")
    if _digest(path) != source["sha256"]:
        raise ValueError("source changed during decode")
    return pcm


def _transcribe(pcm, config):
    import os
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from .transcribe import _local_model
    model = _local_model(config)
    import numpy as np
    import mlx.core as mx
    import mlx_whisper
    try:
        return mlx_whisper.transcribe(np.asarray(pcm, dtype=np.float32),
                                     path_or_hf_repo=str(model), language=config.get("language", "zh"),
                                     word_timestamps=True, verbose=None, temperature=0.0,
                                     condition_on_previous_text=False, fp16=True)
    finally:
        mx.clear_cache()


def _result(entry):
    if not isinstance(entry, dict):
        return None
    if "state" in entry:
        return entry.get("result") if entry["state"] == "succeeded" else None
    return entry


def _edge_speech(result, left):
    duration = result["duration_ms"]
    start, end = (max(0, duration - 250), duration) if left else (0, min(250, duration))
    return any(r.get("kind") == "speech" and r["start_ms"] < end and r["end_ms"] > start
               for r in result.get("regions", []))


def _overlaps(a, b):
    return (a["chunk_id"] == b["chunk_id"] and a["sha256"] == b["sha256"]
            and a["start_ms"] < b["end_ms"] and b["start_ms"] < a["end_ms"])


def _normalized(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def _review(utterance, old, durations, overlap_ms):
    spans = utterance["source_spans"]
    relevant = [u for u in old if any(_overlaps(a, b) for a in u.get("source_spans", []) for b in spans)]
    related_ids = [u["id"] for u in relevant]
    if overlap_ms > 0:
        return [], related_ids, "overlapping_media_requires_review"
    if not relevant or {s["chunk_id"] for u in relevant for s in u["source_spans"]} != set(durations):
        return [], related_ids, "missing_original_coverage"
    if len(set(related_ids)) != len(related_ids):
        return [], related_ids, "ambiguous_original_ids"
    for original in relevant:
        if len(original["source_spans"]) != 1:
            return [], related_ids, "already_cross_source_original"
        original_span = original["source_spans"][0]
        if not any(s["chunk_id"] == original_span["chunk_id"] and s["sha256"] == original_span["sha256"]
                   and s["start_ms"] <= original_span["start_ms"] < original_span["end_ms"] <= s["end_ms"]
                   for s in spans):
            return [], related_ids, "partial_original_sentence"
    if _normalized(utterance["text"]) != _normalized("".join(u["text"] for u in relevant)):
        return [], related_ids, "text_changed_requires_review"
    for span in spans:
        originals = [u["source_spans"][0] for u in relevant if u["source_spans"][0]["chunk_id"] == span["chunk_id"]]
        if (not originals or min(s["start_ms"] for s in originals) - span["start_ms"] > 250
                or span["end_ms"] - max(s["end_ms"] for s in originals) > 250):
            return [], related_ids, "uncovered_model_time"
        originals.sort(key=lambda s: s["start_ms"])
        if any(a["end_ms"] > b["start_ms"] for a, b in zip(originals, originals[1:])):
            return [], related_ids, "overlapping_original_sentences"
    return related_ids, related_ids, "same_text_and_complete_source_coverage"


def reconcile_boundaries(sources: list, analyses: dict, config: dict, data_root: Path) -> dict:
    started = time.monotonic()
    metrics = {"pairs_considered": 0, "pairs_eligible": 0, "asr_calls": 0, "asr_input_ms": 0,
               "reconciled_count": 0, "candidate_count": 0, "empty_asr": 0,
               "non_crossing_segments": 0, "skipped": {}, "errors": []}
    output, replaced, seen_output = [], set(), set()
    loader = config.get("_boundary_audio_loader", _decode_window)
    transcriber = config.get("_boundary_transcriber", _transcribe)
    context_ms = int(config.get("boundary_context_ms", 6000))
    if not 1 <= context_ms <= 6000:
        raise ValueError("boundary_context_ms must be 1..6000")
    ordered = sorted(sources, key=lambda s: (s["epoch"], s["chunk_id"]))

    def skip(reason):
        metrics["skipped"][reason] = metrics["skipped"].get(reason, 0) + 1

    for left, right in zip(ordered, ordered[1:]):
        metrics["pairs_considered"] += 1
        try:
            if left.get("synthetic") or right.get("synthetic"):
                skip("synthetic"); continue
            if (left["chunk_id"] == right["chunk_id"] or left["session_id"] != right["session_id"]
                    or right["sequence"] != left["sequence"] + 1):
                skip("discontinuous_source"); continue
            declared_gap = float(right["epoch"]) - float(left["epoch"]) - float(left["duration_seconds"])
            if not math.isfinite(declared_gap) or abs(declared_gap) > .250001:
                skip("time_gap"); continue
            a, b = _result(analyses.get(left["chunk_id"])), _result(analyses.get(right["chunk_id"]))
            if not a or not b:
                skip("analysis_unavailable"); continue
            da, db = a["duration_ms"], b["duration_ms"]
            if any(not isinstance(d, int) or d <= 0 for d in (da, db)):
                skip("uncalibrated_duration"); continue
            if any(abs(d - float(s["duration_seconds"]) * 1000) > 250 for s, d in ((left, da), (right, db))):
                skip("uncalibrated_duration"); continue
            gap_ms = round((float(right["epoch"]) - float(left["epoch"])) * 1000) - da
            if abs(gap_ms) > 250:
                skip("uncalibrated_media_gap"); continue
            if not _edge_speech(a, True) or not _edge_speech(b, False):
                skip("no_boundary_speech"); continue
            metrics["pairs_eligible"] += 1
            ranges = [(left, max(0, da - context_ms), da), (right, 0, min(db, context_ms))]
            pcm, mapping, position = array("f"), [], 0
            for index, (source, begin, end) in enumerate(ranges):
                if index and gap_ms > 0:
                    pcm.extend(array("f", [0.0]) * (gap_ms * 16))
                    mapping.append({"derived_start_ms": position, "derived_end_ms": position + gap_ms, "chunk_id": None})
                    position += gap_ms
                samples = loader(source, begin, end, config, data_root)
                if len(samples) != (end - begin) * 16 or any(not math.isfinite(v) for v in samples):
                    raise ValueError("invalid calibrated PCM window")
                pcm.extend(samples)
                mapping.append({"derived_start_ms": position, "derived_end_ms": position + end - begin,
                                "chunk_id": source["chunk_id"], "sha256": source["sha256"], "start_ms": begin, "end_ms": end})
                position += end - begin
            metrics["asr_calls"] += 1
            metrics["asr_input_ms"] += position
            response = transcriber(pcm, config)
            if not isinstance(response, dict) or not isinstance(response.get("segments"), list):
                raise ValueError("missing boundary model segments")
            if not response["segments"]:
                metrics["empty_asr"] += 1
                continue
            pair_id = "boundary_" + hashlib.sha256((left["chunk_id"] + left["sha256"] + right["chunk_id"] + right["sha256"]).encode()).hexdigest()[:24]
            old = sorted(a.get("utterances", []), key=lambda u: u["source_spans"][0]["start_ms"])
            old += sorted(b.get("utterances", []), key=lambda u: u["source_spans"][0]["start_ms"])
            proposals = []
            for segment in response["segments"]:
                # Out-of-window timestamps are not silently clamped for revisions.
                if (not math.isfinite(float(segment["start"])) or not math.isfinite(float(segment["end"]))
                        or segment["start"] < 0 or segment["end"] * 1000 > position + 1):
                    raise ValueError("boundary model timestamp outside context")
                u = segment_to_utterance(segment, mapping, position)
                if u is None:
                    continue
                if len(u["source_spans"]) != 2 or any(s["end_ms"] - s["start_ms"] < 100 for s in u["source_spans"]):
                    metrics["non_crossing_segments"] += 1
                    continue
                ids, related, reason = _review(u, old, {left["chunk_id"]: da, right["chunk_id"]: db}, max(0, -gap_ms))
                u.update({"boundary_id": pair_id, "replaces_ids": ids, "related_ids": related, "reason": reason,
                          "epoch": float(left["epoch"]) + u["source_spans"][0]["start_ms"] / 1000})
                proposals.append(u)
            # Whole-pair staging ensures a malformed later segment cannot leave a
            # partially committed replacement. Conflicting outputs stay candidates.
            for u in proposals:
                conflicts = any(v is not u and any(_overlaps(x, y) for x in u["source_spans"] for y in v["source_spans"]) for v in proposals)
                prior_overlap = any(v["boundary_status"] == "reconciled"
                                    and any(_overlaps(x, y) for x in u["source_spans"] for y in v["source_spans"])
                                    for v in output)
                if conflicts or prior_overlap or replaced.intersection(u["related_ids"]):
                    u["replaces_ids"] = []
                    u["reason"] = "conflicting_boundary_outputs"
                if u["id"] in seen_output:
                    continue
                seen_output.add(u["id"])
                accepted = bool(u["replaces_ids"])
                u["boundary_status"] = "reconciled" if accepted else "context_candidate"
                u["review_required"] = not accepted
                replaced.update(u["replaces_ids"])
                metrics["reconciled_count" if accepted else "candidate_count"] += 1
                output.append(u)
        except Exception as exc:
            metrics["errors"].append({"pair_index": metrics["pairs_considered"] - 1, "error_type": type(exc).__name__})
    metrics["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return {"utterances": output, "replaced_ids": sorted(replaced), "metrics": metrics}
