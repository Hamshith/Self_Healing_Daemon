import json
import os
import shutil
import tempfile
import unittest

import remediator


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


if __name__ == "__main__":
    unittest.main()
