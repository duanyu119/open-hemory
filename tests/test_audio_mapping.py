"""Standard-library tests: source time, conservative retention, adapter boundaries."""
import array
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mac"))
from semantic.audio import _decode_pcm, analyze_chunk, classify_frames
from semantic.timeline import asr_windows, map_source_spans, merge_intervals, segment_to_utterance, stable_utterance_id


def source(derived_start, derived_end, original_start, chunk="a"):
    return {"derived_start_ms": derived_start, "derived_end_ms": derived_end,
            "chunk_id": chunk, "sha256": "1" * 64, "start_ms": original_start,
            "end_ms": original_start + derived_end - derived_start}


def frame(start, end, probability=0.001, dbfs=-80):
    return {"start_ms": start, "end_ms": end, "probability": probability, "rms_dbfs": dbfs}


class MediaTimestampTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    def test_aac_pts_offset_does_not_shift_source_timeline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "offset.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1:sample_rate=48000", "-ac", "1",
                            "-c:a", "aac", "-output_ts_offset", "0.5", str(path)], check=True)
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                                        "format=duration", "-of", "json", str(path)]))
            duration = float(probe["format"]["duration"])
            output = Path(folder) / "decoded.f32"
            count = _decode_pcm(path, output, shutil.which("ffmpeg"), duration)
            samples = array.array("f")
            samples.frombytes(output.read_bytes())
            self.assertAlmostEqual(count / 16000, duration, delta=0.002)
            self.assertLess(max(abs(v) for v in samples[:int(.45 * 16000)]), .001)
            self.assertGreater(max(abs(v) for v in samples[int(.55 * 16000):int(.8 * 16000)]), .01)


class SourceMappingTests(unittest.TestCase):
    def test_skipped_audio_maps_to_original_not_compacted_time(self):
        mapping = [source(0, 10000, 0), source(10000, 20000, 40000)]
        spans = map_source_spans(12000, 13500, mapping)
        self.assertEqual((spans[0]["start_ms"], spans[0]["end_ms"]), (42000, 43500))

    def test_cross_chunk_sentence_omits_artificial_gap(self):
        mapping = [source(0, 1000, 299000),
                   {"derived_start_ms": 1000, "derived_end_ms": 1500, "chunk_id": None},
                   source(1500, 3000, 0, "b")]
        spans = map_source_spans(700, 1900, mapping)
        self.assertEqual([(s["chunk_id"], s["start_ms"], s["end_ms"]) for s in spans],
                         [("a", 299700, 300000), ("b", 0, 400)])

    def test_half_open_boundary_never_includes_previous_chunk(self):
        spans = map_source_spans(1000, 1500, [source(0, 1000, 0), source(1000, 2000, 0, "b")])
        self.assertEqual([s["chunk_id"] for s in spans], ["b"])

    def test_mapping_rejects_time_stretch_and_overlap(self):
        invalid = source(0, 1000, 0)
        invalid["end_ms"] = 1100
        with self.assertRaises(ValueError):
            map_source_spans(0, 1000, [invalid])
        with self.assertRaises(ValueError):
            map_source_spans(0, 1000, [source(0, 700, 0), source(600, 1000, 800)])

    def test_segment_uses_model_times_and_stable_ids(self):
        segment = {"text": "Synthetic test phrase", "start": 1.24, "end": 2.56}
        utterance = segment_to_utterance(segment, [source(0, 3000, 40000)], 3000)
        self.assertEqual(utterance["source_spans"][0]["start_ms"], 41240)
        self.assertEqual(utterance["source_spans"][0]["end_ms"], 42560)
        self.assertEqual(utterance, segment_to_utterance(segment, [source(0, 3000, 40000)], 3000))
        changed = dict(segment, text="A different, much longer string cannot change the time")
        self.assertEqual(utterance["source_spans"], segment_to_utterance(changed, [source(0, 3000, 40000)], 3000)["source_spans"])
        self.assertNotEqual(utterance["id"], stable_utterance_id("different", utterance["source_spans"]))

    def test_missing_invalid_or_unmapped_times_fail(self):
        for start, end in [(float("nan"), 1), (2, 1), (-1, 1), (4, 5)]:
            with self.assertRaises(ValueError):
                segment_to_utterance({"text": "synthetic", "start": start, "end": end}, [source(0, 3000, 0)], 3000)
        with self.assertRaises(KeyError):
            segment_to_utterance({"text": "synthetic"}, [source(0, 3000, 0)], 3000)

    def test_encoder_padding_is_clamped_to_actual_media(self):
        u = segment_to_utterance({"text": "synthetic", "start": 2.9, "end": 3.2}, [source(0, 3000, 1000)], 3000)
        self.assertEqual(u["source_spans"][0]["end_ms"], 4000)


class ConservativeVadTests(unittest.TestCase):
    def test_exactly_eight_seconds_of_clear_silence_can_skip(self):
        self.assertEqual(classify_frames([frame(0, 8000)], 8000)[0]["kind"], "silence")
        self.assertEqual(classify_frames([frame(0, 7999)], 7999)[0]["kind"], "uncertain")

    def test_short_reply_is_retained_and_padded(self):
        regions = classify_frames([frame(0, 10000), frame(10000, 10032, .9, -20), frame(10032, 22000)], 22000)
        self.assertEqual([(r["start_ms"], r["end_ms"], r["kind"]) for r in regions],
                         [(0, 9700, "silence"), (9700, 10332, "speech"), (10332, 22000, "silence")])

    def test_possible_voice_and_loud_noise_are_not_removed(self):
        for probability, energy in [(.2, -65), (.001, -12)]:
            regions = classify_frames([frame(0, 20000, probability, energy)], 20000)
            self.assertEqual(regions[0]["kind"], "uncertain")

    def test_short_pauses_merge_without_losing_full_coverage(self):
        regions = classify_frames([frame(0, 500, .8, -20), frame(500, 1200), frame(1200, 1700, .8, -20), frame(1700, 6000)], 6000)
        self.assertEqual(regions[0], {"start_ms": 0, "end_ms": 2000, "kind": "speech", "reason": "silero_speech_with_padding_and_short_pause"})
        self.assertEqual(regions[-1]["end_ms"], 6000)
        self.assertEqual(sum(r["end_ms"] - r["start_ms"] for r in regions), 6000)

    def test_incomplete_vad_or_unsafe_configuration_fails(self):
        with self.assertRaises(ValueError):
            classify_frames([frame(0, 2000)], 3000)
        with self.assertRaises(ValueError):
            classify_frames([frame(0, 3000)], 3000, {"min_silence_ms": 100})

    def test_asr_windows_bounded_and_do_not_bridge_removed_audio(self):
        regions = [{"start_ms": 0, "end_ms": 95000, "kind": "speech"},
                   {"start_ms": 95000, "end_ms": 110000, "kind": "silence"},
                   {"start_ms": 110000, "end_ms": 112000, "kind": "uncertain"}]
        windows = list(asr_windows(regions))
        self.assertTrue(all(w["end_ms"] - w["start_ms"] <= 30000 for w in windows))
        self.assertTrue(all(w["end_ms"] <= 95000 or w["start_ms"] >= 110000 for w in windows))
        self.assertEqual(sum(w["core_end_ms"] - w["core_start_ms"] for w in windows), 97000)

    def test_bad_original_hash_fails_before_importing_models(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.m4a"
            path.write_bytes(b"synthetic non-media fixture")
            metadata = {"chunk_id": "synthetic", "sha256": "0" * 64, "duration_seconds": 1,
                        "started_at": "2026-09-19T00:00:00Z", "session_id": "s", "sequence": 0}
            with self.assertRaisesRegex(ValueError, "SHA256"):
                analyze_chunk(path, metadata, {})

    def test_import_does_not_load_model_packages(self):
        code = ("import sys; sys.path.insert(0, " + repr(str(ROOT / "mac")) + "); "
                "import semantic.audio, semantic.transcribe; "
                "assert not any(m in sys.modules for m in ['numpy','torch','onnxruntime','mlx','mlx_whisper'])")
        subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    unittest.main()
