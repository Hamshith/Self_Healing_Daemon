import json
import os
import shutil
import tempfile
import unittest

import remediator
from unittest.mock import patch


class RemediatorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="remediator-tests-")
        self.original_rollback_dir = remediator.ROLLBACK_DIR
        remediator.ROLLBACK_DIR = self.tmpdir

    def tearDown(self):
        remediator.ROLLBACK_DIR = self.original_rollback_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_memory_to_mi_handles_decimal_units(self):
        self.assertAlmostEqual(remediator._parse_memory_to_mi("512M"), 512 * (1000000 / (1024 * 1024)))
        self.assertAlmostEqual(remediator._parse_memory_to_mi("1Gi"), 1024.0)

    def test_write_rollback_snapshot_includes_namespace(self):
        path = remediator._write_rollback_snapshot(
            "demo-pod",
            "patch_memory",
            {"deployment_name": "demo-deploy", "namespace": "demo-ns"},
        )
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertEqual(loaded["namespace"], "demo-ns")

    def test_llm_steps_select_memory_patch(self):
        incident = {
            "pod_name": "oom-pod",
            "namespace": "default",
            "fault_type": "OOMKilled",
        }
        diagnosis = {
            "root_cause_category": "OOMKilled",
            "safe_to_auto_remediate": True,
            "remediation_steps": [{
                "step": 1,
                "action": "increase_memory_limit",
                "parameters": {"increase_pct": 0.25},
            }],
        }

        with patch.object(remediator, "_patch_memory_limit", return_value={
            "status": "remediated",
        }) as patch_memory:
            result = remediator.remediate(incident, diagnosis)

        patch_memory.assert_called_once_with(incident, diagnosis, increase_pct=0.25)
        self.assertEqual(result["status"], "remediated")

    def test_safe_false_does_not_execute_llm_steps(self):
        incident = {"pod_name": "oom-pod", "namespace": "default"}
        diagnosis = {
            "safe_to_auto_remediate": False,
            "remediation_steps": [{"step": 1, "action": "delete_pod"}],
        }

        with patch.object(remediator, "_delete_pod") as delete_pod:
            result = remediator.remediate(incident, diagnosis)

        delete_pod.assert_not_called()
        self.assertEqual(result["status"], "escalated")


if __name__ == "__main__":
    unittest.main()
