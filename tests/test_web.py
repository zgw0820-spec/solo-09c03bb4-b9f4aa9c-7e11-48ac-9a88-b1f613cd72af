from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from linklab.runner import FakeRunner  # noqa: E402
from linklab.tc_argv import Topology  # noqa: E402
from linklab.tc_manager import TcManager  # noqa: E402
from linklab.web import create_app  # noqa: E402


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        topo = Topology(src_netns=None, dst_netns=None)
        self.mgr = TcManager(
            topo, FakeRunner(), Path(self._tmp.name) / "state.json"
        )
        self.app = create_app(manager=self.mgr, flows=None, start_runtime=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_index_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("园区上行调速台", body)
        self.assertIn("alarm_rate_mbps", body)

    def test_state_api(self):
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["config"]["total_kbps"], 10_000)

    def test_validate_endpoint_reports_plan(self):
        r = self.client.post(
            "/api/config/validate",
            json={
                "total_mbps": 8,
                "alarm_rate_mbps": 1.6,
                "alarm_ceil_mbps": 8,
                "video_rate_mbps": 4.8,
                "video_ceil_mbps": 8,
            },
        )
        data = r.get_json()
        self.assertTrue(data["valid"], data)
        self.assertGreaterEqual(data["plan"]["step_count"], 1)
        for s in data["plan"]["steps"]:
            self.assertTrue(s["invariant_ok"])
            self.assertIn("tc", s["argv"])

    def test_validate_rejects_bad_input(self):
        r = self.client.post(
            "/api/config/validate",
            json={
                "total_mbps": 10,
                "alarm_rate_mbps": 8,
                "alarm_ceil_mbps": 10,
                "video_rate_mbps": 8,
                "video_ceil_mbps": 10,
            },
        )
        data = r.get_json()
        self.assertFalse(data["valid"])
        self.assertTrue(data["errors"])

    def test_apply_then_state_changes(self):
        r = self.client.post(
            "/api/config/apply",
            json={
                "total_mbps": 12,
                "alarm_rate_mbps": 3,
                "alarm_ceil_mbps": 12,
                "video_rate_mbps": 7,
                "video_ceil_mbps": 12,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["config"]["total_kbps"], 12_000)

    def test_apply_invalid_returns_409_and_keeps_state(self):
        r = self.client.post(
            "/api/config/apply",
            json={
                "total_mbps": 10,
                "alarm_rate_mbps": 9,
                "alarm_ceil_mbps": 10,
                "video_rate_mbps": 9,
                "video_ceil_mbps": 10,
            },
        )
        self.assertEqual(r.status_code, 409)
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["rejected"])
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["config"]["total_kbps"], 10_000)

    def test_demo_failure_endpoint_rolls_back(self):
        r = self.client.post("/api/demo/failure")
        self.assertEqual(r.status_code, 409)
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["rollback"]["ok"])
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["config"]["total_kbps"], 10_000)

    def test_flows_disabled_shape(self):
        r = self.client.get("/api/flows")
        data = r.get_json()
        self.assertTrue(data["disabled"])

    def test_remark_endpoint_disabled_without_runtime(self):
        r = self.client.post(
            "/api/flows/alarm/remark", json={"new_dscp": 20}
        )
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])

    def test_remark_unknown_flow_404(self):
        r = self.client.post(
            "/api/flows/nope/remark", json={"new_dscp": 20}
        )
        self.assertEqual(r.status_code, 404)

    def test_index_contains_dscp_panel(self):
        r = self.client.get("/")
        body = r.get_data(as_text=True)
        self.assertIn("DSCP 换标核验", body)
        self.assertIn("dscp-alarm-input", body)

    def test_kernel_endpoint_with_fake_runner(self):
        r = self.client.get("/api/kernel")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["sections"])
        self.assertIn("state", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
