import unittest

from app.ui import preparation_log


class PreparationLogTest(unittest.TestCase):
    def test_empty_heartbeats_are_hidden_but_steps_and_actor_labels_remain(self):
        job = {"log": [
            {"kind": "progress", "status": "running", "text": ""},
            {"phase": "review", "progress": "Reviewing the grader", "detail": "Separate review"},
            {"kind": "message", "status": "running", "label": "actor started"},
            {"kind": "progress", "text": "Checking controls"},
            {"phase": "reference_result", "progress": "Reference check: passed"},
        ]}
        html = preparation_log(job)
        self.assertEqual(html.count("<li>"), 4)
        self.assertIn("Reviewing the grader — Separate review", html)
        self.assertIn("actor started", html)
        self.assertIn("Checking controls", html)
        self.assertIn("Reference check: passed", html)
        self.assertEqual(len(job["log"]), 5)

    def test_messages_are_escaped_and_phase_only_records_are_supported(self):
        html = preparation_log({"log": [
            {"phase": "setup", "detail": "<script>"},
            {"kind": "message", "text": "<b>result</b>"},
            {},
        ]})
        self.assertEqual(html.count("<li>"), 2)
        self.assertIn("setup — &lt;script&gt;", html)
        self.assertIn("&lt;b&gt;result&lt;/b&gt;", html)

    def test_detail_only_records_are_not_lost(self):
        self.assertIn("Trouble checking controls", preparation_log({"log": [
            {"detail": "Trouble checking controls"},
        ]}))
        self.assertIn("Trouble checking controls", preparation_log({"log": [
            {"kind": "progress", "text": "", "detail": "Trouble checking controls"},
        ]}))

    def test_no_meaningful_events(self):
        for job in ({}, {"log": []}, {"log": [{"kind": "progress", "text": ""}]}):
            with self.subTest(job=job):
                self.assertIn("No log.", preparation_log(job))
