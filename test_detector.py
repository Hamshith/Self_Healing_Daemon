import unittest
from types import SimpleNamespace
from unittest.mock import patch

from detector import _detect_cpu_throttle, _detect_network_latency


class DetectCpuThrottleTests(unittest.TestCase):
    def test_cpu_throttle_anomaly_has_restart_count(self):
        anomaly = {
            "pod_name": "demo-pod",
            "namespace": "default",
            "fault_type": "CPUThrottle",
            "restart_count": 0,
            "container_name": "demo-container",
            "cpu_usage_millicores": 950,
            "cpu_limit_millicores": 1000,
        }
        self.assertIn("restart_count", anomaly)
        self.assertEqual(anomaly["restart_count"], 0)


class DetectNetworkLatencyTests(unittest.TestCase):
    def test_detect_network_latency_requires_live_target_pod(self):
        class FakeCoreV1:
            def list_pod_for_all_namespaces(self, watch=False):
                return SimpleNamespace(items=[SimpleNamespace(
                    metadata=SimpleNamespace(
                        namespace="default",
                        name="nginx-abc",
                        labels={"app": "nginx"},
                    )
                )])

        class FakeCustomObjects:
            def list_cluster_custom_object(self, **kwargs):
                return {
                    "items": [
                        {
                            "metadata": {"name": "chaos-creater", "namespace": "default"},
                            "spec": {
                                "action": "delay",
                                "selector": {
                                    "namespaces": ["default"],
                                    "labelSelectors": {"app": "nginx"},
                                },
                            },
                            "status": {"conditions": [{"type": "AllInjected", "status": "True"}]},
                        }
                    ]
                }

        with patch("detector._measure_http_latency", return_value=500.0):
            anomalies = _detect_network_latency(FakeCustomObjects(), FakeCoreV1())
            repeated_anomalies = _detect_network_latency(FakeCustomObjects(), FakeCoreV1())
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(len(repeated_anomalies), 1)
        self.assertEqual(anomalies[0]["target_app"], "nginx")
        self.assertIn("nginx-abc", anomalies[0]["target_pods"])

    def test_detect_network_latency_accepts_running_phase_with_conditions(self):
        class FakeCoreV1:
            def list_pod_for_all_namespaces(self, watch=False):
                return SimpleNamespace(items=[SimpleNamespace(
                    metadata=SimpleNamespace(
                        namespace="default",
                        name="nginx-abc",
                        labels={"app": "nginx"},
                    )
                )])

        class FakeCustomObjects:
            def list_cluster_custom_object(self, **kwargs):
                return {
                    "items": [{
                        "metadata": {"name": "chaos-creater", "namespace": "default"},
                        "spec": {
                            "action": "delay",
                            "selector": {
                                "namespaces": ["default"],
                                "labelSelectors": {"app": "nginx"},
                            },
                        },
                        "status": {
                            "phase": "Running",
                            "conditions": [{"type": "Paused", "status": "False"}],
                        },
                    }]
                }

        with patch("detector._measure_http_latency", return_value=500.0):
            anomalies = _detect_network_latency(FakeCustomObjects(), FakeCoreV1())
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["fault_type"], "NetworkLatency")

    def test_detect_network_latency_ignores_measurement_below_threshold(self):
        class FakeCoreV1:
            def list_pod_for_all_namespaces(self, watch=False):
                return SimpleNamespace(items=[SimpleNamespace(
                    metadata=SimpleNamespace(
                        namespace="default",
                        name="nginx-abc",
                        labels={"app": "nginx"},
                    )
                )])

        class FakeCustomObjects:
            def list_cluster_custom_object(self, **kwargs):
                return {
                    "items": [{
                        "metadata": {"name": "chaos-creater", "namespace": "default"},
                        "spec": {
                            "action": "delay",
                            "selector": {
                                "namespaces": ["default"],
                                "labelSelectors": {"app": "nginx"},
                            },
                        },
                        "status": {"conditions": [{"type": "AllInjected", "status": "True"}]},
                    }]
                }

        with patch("detector._measure_http_latency", return_value=100.0):
            anomalies = _detect_network_latency(FakeCustomObjects(), FakeCoreV1())
        self.assertEqual(anomalies, [])


if __name__ == "__main__":
    unittest.main()
