import unittest
from unittest.mock import patch

import llm_client


class DiagnoseIncidentFallbackTests(unittest.TestCase):
    def setUp(self):
        self.incident = {
            "pod_name": "demo-pod",
            "namespace": "default",
            "restart_count": 1,
            "fault_type": "OOMKilled",
        }
        self.qwen_diagnosis = {
            "root_cause": "The container exceeded its memory limit.",
            "root_cause_category": "OOMKilled",
            "safe_to_auto_remediate": False,
            "remediation_steps": [{"step": 1, "action": "escalate"}],
        }
        prompt_patcher = patch.object(
            llm_client,
            "_build_user_prompt",
            return_value="test incident prompt",
        )
        prompt_patcher.start()
        self.addCleanup(prompt_patcher.stop)

    @patch.object(llm_client.qwen_fallback, "diagnose")
    @patch.object(llm_client, "_call_gemini", side_effect=TimeoutError("Gemini timeout"))
    def test_api_failure_uses_qwen_fallback(self, _call_gemini, diagnose):
        diagnose.return_value = self.qwen_diagnosis

        result = llm_client.diagnose_incident(self.incident)

        self.assertIs(result, self.qwen_diagnosis)
        self.assertEqual(_call_gemini.call_count, 1)
        diagnose.assert_called_once_with(self.incident)

    @patch.object(llm_client.qwen_fallback, "diagnose")
    @patch.object(
        llm_client,
        "_call_gemini",
        side_effect=["not JSON", "still not JSON"],
    )
    def test_invalid_json_after_retry_uses_qwen_fallback(
        self, _call_gemini, diagnose
    ):
        diagnose.return_value = self.qwen_diagnosis

        result = llm_client.diagnose_incident(self.incident)

        self.assertIs(result, self.qwen_diagnosis)
        self.assertEqual(_call_gemini.call_count, 2)
        diagnose.assert_called_once_with(self.incident)

    @patch.object(llm_client.qwen_fallback, "diagnose")
    @patch.object(
        llm_client,
        "_call_gemini",
        return_value='{"root_cause_category": "OOMKilled"}',
    )
    def test_valid_gemini_response_does_not_call_qwen(
        self, _call_gemini, diagnose
    ):
        result = llm_client.diagnose_incident(self.incident)

        self.assertEqual(result["root_cause_category"], "OOMKilled")
        self.assertEqual(_call_gemini.call_count, 1)
        diagnose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
