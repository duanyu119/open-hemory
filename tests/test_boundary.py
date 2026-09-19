"""Boundary reconciliation tests; models replaced by explicit in-process fixtures.

Run: python3.13 -m unittest discover -s tests -p 'test_boundary.py' -v
One optional FFmpeg test checks seeked AAC sample alignment against full sequential
PTS-preserving decode. No private media or network is used by this suite.
"""
from array import array
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mac"))
from semantic.boundary import _decode_window, reconcile_boundaries


def original(identifier, text, source, start, end):
    return {"id": identifier, "text": text, "timing_quality": "model_segment",
            "source_spans": [{"chunk_id": source["chunk_id"], "sha256": source["sha256"], "start_ms": start, "end_ms": end}]}


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.sources = [{"chunk_id": "left", "sha256": "a" * 64, "epoch": 0.0, "duration_seconds": 10,
                         "session_id": "same", "sequence": 0},
                        {"chunk_id": "right", "sha256": "b" * 64, "epoch": 10.0, "duration_seconds": 10,
                         "session_id": "same", "sequence": 1}]
        self.analyses = {}
        for index, source in enumerate(self.sources):
            utterance = original("u" + str(index), "跨片" if index == 0 else "句子", source,
                                 8000 if index == 0 else 0, 10000 if index == 0 else 2000)
            self.analyses[source["chunk_id"]] = {"state": "succeeded", "result": {
                "duration_ms": 10000, "regions": [{"start_ms": 0, "end_ms": 10000, "kind": "speech"}],
                "utterances": [utterance]}}
        self.calls = []
        self.segments = [{"text": "跨片句子", "start": 4.0, "end": 8.0}]
        def loader(source, start, end, config, root):
            self.calls.append((source["chunk_id"], start, end))
            return array("f", [1.0]) * ((end - start) * 16)
        def transcriber(pcm, config):
            self.input_pcm = pcm
            return {"segments": self.segments}
        self.config = {"_boundary_audio_loader": loader, "_boundary_transcriber": transcriber}

    def run_boundary(self):
        return reconcile_boundaries(self.sources, self.analyses, self.config, Path("/unused-injected"))

    def test_complete_identical_cross_source_sentence_replaces_with_stable_mapping(self):
        before = copy.deepcopy((self.sources, self.analyses))
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], ["u0", "u1"])
        u = result["utterances"][0]
        self.assertEqual(u["boundary_status"], "reconciled")
        self.assertEqual(u["epoch"], 8)
        self.assertEqual([(s["chunk_id"], s["start_ms"], s["end_ms"]) for s in u["source_spans"]],
                         [("left", 8000, 10000), ("right", 0, 2000)])
        self.assertEqual(self.calls, [("left", 4000, 10000), ("right", 0, 6000)])
        self.assertEqual(before, (self.sources, self.analyses))
        self.assertEqual(u["id"], self.run_boundary()["utterances"][0]["id"])

    def test_positive_gap_has_no_fabricated_source(self):
        self.sources[1]["epoch"] = 10.125
        self.segments[0]["end"] = 8.125
        result = self.run_boundary()
        spans = result["utterances"][0]["source_spans"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[1]["end_ms"], 2000)
        self.assertEqual(result["metrics"]["asr_input_ms"], 12125)
        self.assertTrue(all(x == 0 for x in self.input_pcm[6000 * 16:6125 * 16]))

    def test_changed_text_only_returns_review_candidate(self):
        self.segments[0]["text"] = "识别得到新的文字"
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        u = result["utterances"][0]
        self.assertEqual(u["boundary_status"], "context_candidate")
        self.assertTrue(u["review_required"])
        self.assertEqual(u["related_ids"], ["u0", "u1"])
        self.assertEqual(u["replaces_ids"], [])

    def test_word_boundary_whitespace_is_not_treated_as_identical_text(self):
        self.analyses["left"]["result"]["utterances"][0]["text"] = "now "
        self.analyses["right"]["result"]["utterances"][0]["text"] = "here"
        self.segments[0]["text"] = "nowhere"
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(result["utterances"][0]["boundary_status"], "context_candidate")

    def test_partial_original_sentence_is_never_deleted(self):
        self.analyses["left"]["result"]["utterances"][0]["source_spans"][0]["start_ms"] = 7000
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(result["utterances"][0]["reason"], "partial_original_sentence")

    def test_overlapping_media_remains_candidate(self):
        self.sources[1]["epoch"] = 9.9
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(result["utterances"][0]["reason"], "overlapping_media_requires_review")

    def test_discontinuity_skips_without_loading_or_asr(self):
        for field, value in [("sequence", 2), ("session_id", "different"), ("epoch", 10.251), ("epoch", 9.7)]:
            with self.subTest(field=field, value=value):
                old = self.sources[1][field]
                self.sources[1][field] = value
                result = self.run_boundary()
                self.assertEqual(result["metrics"]["asr_calls"], 0)
                self.assertEqual(result["utterances"], [])
                self.assertEqual(self.calls, [])
                self.sources[1][field] = old

    def test_uncertain_alone_does_not_trigger_boundary_asr(self):
        self.analyses["right"]["result"]["regions"][0]["kind"] = "uncertain"
        self.assertEqual(self.run_boundary()["metrics"]["asr_calls"], 0)
        self.assertEqual(self.calls, [])

    def test_missing_analysis_or_uncalibrated_duration_skips(self):
        self.analyses["right"]["state"] = "failed"
        self.assertEqual(self.run_boundary()["metrics"]["asr_calls"], 0)
        self.analyses["right"]["state"] = "succeeded"
        self.analyses["left"]["result"]["duration_ms"] = 11000
        self.assertEqual(self.run_boundary()["metrics"]["asr_calls"], 0)

    def test_empty_asr_preserves_originals(self):
        self.segments = []
        before = copy.deepcopy(self.analyses)
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(result["utterances"], [])
        self.assertEqual(result["metrics"]["empty_asr"], 1)
        self.assertEqual(before, self.analyses)

    def test_failure_isolated_and_does_not_leak_error_text(self):
        def fail(pcm, config):
            raise RuntimeError("private-looking test payload must not be returned")
        self.config["_boundary_transcriber"] = fail
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(result["metrics"]["errors"], [{"pair_index": 0, "error_type": "RuntimeError"}])
        self.assertNotIn("private-looking", json.dumps(result))

    def test_invalid_later_segment_discards_entire_pair_replacement(self):
        self.segments.append({"text": "invalid timestamp fixture", "start": 11, "end": 20})
        result = self.run_boundary()
        self.assertEqual(result["utterances"], [])
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(len(result["metrics"]["errors"]), 1)

    def test_non_crossing_segment_cannot_replace_original(self):
        self.segments = [{"text": "single source", "start": 1, "end": 2}]
        result = self.run_boundary()
        self.assertEqual(result["utterances"], [])
        self.assertEqual(result["replaced_ids"], [])

    def test_conflicting_crossing_segments_are_candidates(self):
        self.segments.append({"text": "another hypothesis", "start": 4, "end": 8})
        result = self.run_boundary()
        self.assertEqual(result["replaced_ids"], [])
        self.assertEqual(result["metrics"]["candidate_count"], 2)

    def test_import_has_no_model_dependencies(self):
        code = "import sys;sys.path.insert(0," + repr(str(ROOT / "mac")) + ");import semantic.boundary;assert not any(n in sys.modules for n in ('numpy','mlx','mlx_whisper','torch','onnxruntime'))"
        subprocess.run([sys.executable, "-c", code], check=True)


class PartialDecodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "requires FFmpeg")
    def test_seeked_aac_matches_original_pts_and_sample_count(self):
        from semantic.audio import _decode_pcm
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = str(uuid.uuid4())
            folder = root / "chunks" / identifier
            folder.mkdir(parents=True)
            path = folder / (identifier + ".m4a")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "aevalsrc=0.1*sin(2*PI*(200*t+20*t*t)):s=48000:d=12", "-ac", "1", "-c:a", "aac",
                            "-output_ts_offset", "0.5", str(path)], check=True)
            source = {"chunk_id": identifier, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            full_path = root / "reference.f32"
            _decode_pcm(path, full_path, shutil.which("ffmpeg"), 12.5)
            full = array("f"); full.frombytes(full_path.read_bytes())
            partial = _decode_window(source, 8000, 11000, {}, root)
            self.assertEqual(len(partial), 48000)
            # A fresh 48k->16k resampler can differ by a fractional output sample
            # in filter phase. Require agreement within ONE 16k sample (62.5us),
            # well below the 1ms source contract. The chirp avoids periodic aliases.
            reference = full[8000 * 16:11000 * 16]
            errors = []
            for step in range(-8, 9):
                shift = step / 8
                total = 0.0
                count = 0
                for i in range(1602, len(partial) - 2, 7):
                    point = i + shift
                    index = int(point)
                    fraction = point - index
                    aligned = reference[index] * (1 - fraction) + reference[index + 1] * fraction
                    total += (partial[i] - aligned) ** 2
                    count += 1
                errors.append((total / count) ** .5)
            self.assertLess(min(errors), .002)
            head = _decode_window(source, 0, 1000, {}, root)
            self.assertTrue(all(abs(v) < .001 for v in head[:int(.45 * 16000)]))


if __name__ == "__main__":
    unittest.main()
