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

    def test_verified_oom_is_patched_when_llm_safe_flag_is_false(self):
        incident = {
            "pod_name": "oom-pod",
            "namespace": "default",
            "fault_type": "OOMKilled",
        }
        diagnosis = {
            "root_cause_category": "OOMKilled",
            "safe_to_auto_remediate": False,
        }

        with patch.object(remediator, "_patch_memory_limit", return_value={
            "status": "remediated",
        }) as patch_memory:
            result = remediator.remediate(incident, diagnosis)

        patch_memory.assert_called_once_with(incident, diagnosis)
        self.assertEqual(result["status"], "remediated")


if __name__ == "__main__":
    unittest.main()
