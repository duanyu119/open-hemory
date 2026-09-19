"""Regression guardrails for malformed model output (review PR-02).

These encode the review's six probes plus the specific failures the review
flagged: a non-object topic used to raise an uncaught AttributeError, and topic
core evidence could reference a different topic. All must now fail as a
controlled SummaryValidationError (a ValueError subclass), never crash the worker.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mac"))
from semantic.summarize import validate_summary, SummaryValidationError


class SummaryGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.value = {
            "title": "合成测试讨论",
            "overview": "只用于校验结构，不是真实谈话。",
            "topics": [
                {"title": "主题甲", "utterance_ids": ["u1"], "key_points": []},
                {"title": "主题乙", "utterance_ids": ["u2"], "key_points": []},
            ],
        }

    def test_non_object_topic_is_validation_error(self):
        value = copy.deepcopy(self.value)
        value["topics"] = [1]
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1", "u2"])

    def test_null_topic_is_validation_error(self):
        value = copy.deepcopy(self.value)
        value["topics"] = [None]
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1", "u2"])

    def test_topic_core_evidence_must_belong_to_topic(self):
        value = copy.deepcopy(self.value)
        value["topics"][0]["key_points"] = [
            {"text": "主题甲的核心要点", "evidence_ids": ["u2"]}
        ]
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1", "u2"])

    def test_unknown_evidence_id_rejected(self):
        value = copy.deepcopy(self.value)
        value["topics"][0]["key_points"] = [
            {"text": "合成要点", "evidence_ids": ["does_not_exist"]}
        ]
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1", "u2"])

    def test_top_level_evidence_may_span_topics(self):
        value = copy.deepcopy(self.value)
        value["key_points"] = [{"text": "全局要点", "evidence_ids": ["u2"]}]
        result = validate_summary(value, ["u1", "u2"])
        self.assertEqual(result["key_points"][0]["evidence_ids"], ["u2"])

    def test_wrong_field_type_rejected(self):
        value = copy.deepcopy(self.value)
        value["topics"][0]["utterance_ids"] = "u1"
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1", "u2"])

    def test_empty_topics_rejected(self):
        value = copy.deepcopy(self.value)
        value["topics"] = []
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1", "u2"])

    def test_non_dict_summary_rejected(self):
        with self.assertRaises(SummaryValidationError):
            validate_summary([1, 2], ["u1"])

    def test_oversized_topics_rejected(self):
        value = copy.deepcopy(self.value)
        value["topics"] = [{"title": "t", "utterance_ids": ["u1"], "key_points": []}] * 31
        with self.assertRaises(SummaryValidationError):
            validate_summary(value, ["u1"])

    def test_summary_validation_error_is_value_error(self):
        # Backward compatible: existing except ValueError handlers still apply.
        self.assertTrue(issubclass(SummaryValidationError, ValueError))


if __name__ == "__main__":
    unittest.main()
