"""Real process test: sender -> loopback -> receiver, checks measured Mbps.

This exercises the actual standalone scripts (no mocks) and is the local
proof that throughput measurement works.  tc enforcement itself can only
be verified where CAP_NET_ADMIN exists (the Docker Compose host).
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "service" / "linklab"


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class UdpLoopbackTests(unittest.TestCase):
    def test_sender_receiver_measures_traffic(self):
        port = free_udp_port()
        tmp = tempfile.TemporaryDirectory()
        stats = Path(tmp.name) / "recv.json"
        sender_stats = Path(tmp.name) / "send.json"

        recv = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT_DIR / "udp_recv.py"),
                "--flow",
                f"test:{port}",
                "--stats-file",
                str(stats),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            time.sleep(0.8)  # let socket bind + writer start
            send = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "udp_send.py"),
                    "--dst-ip",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--flow",
                    "test",
                    "--rate",
                    "8mbit",
                    "--stats-file",
                    str(sender_stats),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                time.sleep(4.0)
            finally:
                send.terminate()
                try:
                    send.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    send.kill()

            # Give the receiver writer a moment to flush the 1s sample.
            deadline = time.time() + 3
            data = None
            while time.time() < deadline:
                try:
                    data = json.loads(stats.read_text())
                    if data["flows"]["test"]["packets_total"] > 200:
                        break
                except (OSError, json.JSONDecodeError, KeyError):
                    pass
                time.sleep(0.4)

            self.assertIsNotNone(data, "receiver never wrote stats")
            flow = data["flows"]["test"]
            self.assertGreater(flow["packets_total"], 200)
            # 8 Mbit/s offered for ~4s at 1200B ~= 3300 packets.
            # Generous lower bound for slow/contended CI sandboxes.
            self.assertGreater(
                flow["rate_mbps"], 2.0, f"measured too low: {flow}"
            )
        finally:
            recv.terminate()
            try:
                recv.wait(timeout=3)
            except subprocess.TimeoutExpired:
                recv.kill()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
