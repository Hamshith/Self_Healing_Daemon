import unittest
from types import SimpleNamespace

from detector import _detect_cpu_throttle, _detect_network_latency, _previously_flagged_network


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
    def setUp(self):
        _previously_flagged_network.clear()

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

        anomalies = _detect_network_latency(FakeCustomObjects(), FakeCoreV1())
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["target_app"], "nginx")
        self.assertIn("nginx-abc", anomalies[0]["target_pods"])


if __name__ == "__main__":
    unittest.main()
