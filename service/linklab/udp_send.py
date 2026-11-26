#!/usr/bin/env python3
"""Standalone paced UDP test sender (run inside lab-src netns).

Self-contained on purpose: it is launched by path via
``ip netns exec ... python udp_send.py`` so package imports are avoided.

Datagram layout: ``!4sBQd`` header + zero padding to --size bytes.
Writes a small JSON stats file once per second.

DSCP re-marking
---------------
Every sender starts with IPv4 TOS = DSCP 0 / ECT(0) (byte ``0x01``).
When ``--ctl-file`` is given, a small watcher thread polls that JSON file
for a re-mark request ``{"request_id", "new_dscp"}``.  On the first request
of a run it calls ``setsockopt(IP_TOS)`` *while traffic keeps flowing*; the
two ECN bits are always preserved.  The sequence number of the first packet
sent with the new marker is published as the boundary in the stats file.
If ``setsockopt`` fails the sender keeps the old marker, records the reason
and answers ``applied=false`` -- packets and sequence numbers continue.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dscp import (  # noqa: E402
    START_DSCP,
    START_ECN,
    ecn_name,
    tos_byte,
    validate_new_dscp,
)

MAGIC = b"LL01"
HEADER = struct.Struct("!4sBQd")  # magic, flow-id byte, seq u64, send ts double

_running = True


def _stop(signum, frame):  # noqa: ANN001
    global _running
    _running = False


def parse_rate_bps(text: str) -> int:
    s = str(text).strip().lower()
    if s in ("0", "max", "flood"):
        return 0
    mult = 1
    for suf, f in (
        ("gbit", 1_000_000_000),
        ("mbit", 1_000_000),
        ("kbit", 1_000),
    ):
        if s.endswith(suf):
            s = s[: -len(suf)]
            mult = f
            break
    else:
        mult = 1_000_000  # bare number = Mbit/s
    return max(0, int(float(s) * mult))


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class MarkerController:
    """Owns the current TOS marker and applies at most one re-mark per run."""

    def __init__(self, sock: socket.socket, stats_path: Path, ctl_path: Path | None):
        self.sock = sock
        self.stats_path = stats_path
        self.ctl_path = ctl_path
        self.lock = threading.Lock()
        # Every trial starts at DSCP 0 / ECT(0).
        self.dscp = START_DSCP
        self.ecn = START_ECN
        self.tos = tos_byte(self.dscp, self.ecn)
        self.applied_request_id: str | None = None
        self.pending_request_id: str | None = None
        # Boundary: seq of the first packet carrying the new marker.
        self.boundary_seq: int | None = None
        self.remark_error: str | None = None
        self.remark_applied = False
        self._watcher: threading.Thread | None = None

    def apply_initial(self) -> None:
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, self.tos)
        except OSError as e:
            # Without a usable TOS option the trial cannot prove marking;
            # keep sending so throughput behaviour is still observable.
            self.remark_error = f"初始 IP_TOS 设置失败: {e}"

    def start_watcher(self) -> None:
        if self.ctl_path is None:
            return
        self._watcher = threading.Thread(
            target=self._watch_loop, name="dscp-ctl", daemon=True
        )
        self._watcher.start()

    def _watch_loop(self) -> None:
        assert self.ctl_path is not None
        while _running:
            try:
                doc = json.loads(self.ctl_path.read_text(encoding="utf-8"))
                request_id = str(doc.get("request_id", ""))
                # At most one re-mark per sending round; once applied the
                # sender ignores every later request until it is restarted.
                if request_id and self.applied_request_id is None and (
                    request_id != self.pending_request_id
                ):
                    with self.lock:
                        if (
                            self.applied_request_id is None
                            and request_id != self.pending_request_id
                        ):
                            self._handle_request(request_id, doc)
            except FileNotFoundError:
                pass
            except (OSError, json.JSONDecodeError, ValueError):
                # A torn/garbage control file must never kill the sender.
                pass
            time.sleep(0.05)

    def _handle_request(self, request_id: str, doc: dict) -> None:
        try:
            new_dscp = validate_new_dscp(doc.get("new_dscp"))
        except ValueError as e:
            self.remark_error = str(e)
            self.pending_request_id = request_id
            return
        new_tos = tos_byte(new_dscp, self.ecn)  # ECN two bits preserved
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, new_tos)
        except OSError as e:
            # Re-mark failed: keep the old marker and report the reason.
            self.remark_error = (
                f"setsockopt(IP_TOS, 0x{new_tos:02x}) 失败，继续使用旧标记 "
                f"DSCP {self.dscp}: {e}"
            )
            self.pending_request_id = request_id
            return
        # The very next sendto() in the main loop picks this up and records
        # the boundary; the lock makes that hand-off race-free.
        self.dscp = new_dscp
        self.tos = new_tos
        self.applied_request_id = request_id
        self.pending_request_id = None
        self.remark_applied = True

    def before_send(self, seq: int) -> None:
        """Called by the main loop immediately before each sendto()."""
        if self.boundary_seq is None and self.remark_applied:
            with self.lock:
                if self.boundary_seq is None and self.remark_applied:
                    self.boundary_seq = seq

    def stats_block(self) -> dict:
        with self.lock:
            return {
                "dscp": self.dscp,
                "ecn": self.ecn,
                "ecn_name": ecn_name(self.ecn),
                "tos_byte": self.tos,
                "start_dscp": START_DSCP,
                "start_ecn": START_ECN,
                "remark_applied": self.remark_applied,
                "remark_request_id": self.applied_request_id,
                "pending_request_id": self.pending_request_id,
                "boundary_seq": self.boundary_seq,
                "remark_error": self.remark_error,
            }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dst-ip", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--flow", required=True)
    ap.add_argument("--flow-id", type=int, default=1)
    ap.add_argument("--rate", default="12mbit", help="offered rate, 0=flood")
    ap.add_argument("--size", type=int, default=1200)
    ap.add_argument("--stats-file", required=True)
    ap.add_argument(
        "--ctl-file",
        default=None,
        help="optional JSON file polled for a DSCP re-mark request",
    )
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    rate_bps = parse_rate_bps(args.rate)
    dgram_size = max(HEADER.size + 1, args.size)
    pps = rate_bps / 8.0 / dgram_size if rate_bps else 0.0
    interval = 1.0 / pps if pps > 0 else 0.0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    except OSError:
        pass

    stats_path = Path(args.stats_file)
    ctl_path = Path(args.ctl_file) if args.ctl_file else None
    marker = MarkerController(sock, stats_path, ctl_path)
    marker.apply_initial()
    marker.start_watcher()

    pid = os.getpid()
    started = time.time()
    seq = 0
    sent_packets = 0
    sent_bytes = 0
    errors = 0
    last_stat_t = started
    last_stat_bytes = 0

    def write_stats(now: float, send_rate_bps: float) -> None:
        try:
            atomic_json(
                stats_path,
                {
                    "flow": args.flow,
                    "pid": pid,
                    "running": _running,
                    "dst": f"{args.dst_ip}:{args.port}",
                    "offered_rate_bps": rate_bps,
                    "send_rate_bps": int(send_rate_bps),
                    "send_rate_mbps": round(send_rate_bps / 1e6, 3),
                    "sent_packets": sent_packets,
                    "sent_bytes": sent_bytes,
                    "errors": errors,
                    "seq": seq,
                    "started_at": started,
                    "updated_at": now,
                    "marking": marker.stats_block(),
                },
            )
        except OSError:
            pass

    write_stats(started, 0.0)
    next_send = time.perf_counter()

    while _running:
        now_wall = time.time()
        pkt = HEADER.pack(MAGIC, args.flow_id & 0xFF, seq, now_wall)
        if len(pkt) < dgram_size:
            pkt += b"\0" * (dgram_size - len(pkt))
        marker.before_send(seq)
        try:
            sock.sendto(pkt, (args.dst_ip, args.port))
            sent_packets += 1
            sent_bytes += dgram_size
        except OSError:
            errors += 1
            # In flood mode a transient ENOBUFS must not spin hotly.
            time.sleep(0.002)
        seq += 1

        if interval:
            next_send += interval
            delay = next_send - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.05:
                # We fell far behind; reschedule instead of bursting.
                next_send = time.perf_counter()

        now = time.time()
        if now - last_stat_t >= 1.0:
            window_bytes = sent_bytes - last_stat_bytes
            dt = max(1e-6, now - last_stat_t)
            write_stats(now, window_bytes * 8.0 / dt)
            last_stat_t = now
            last_stat_bytes = sent_bytes

    end = time.time()
    dt = max(1e-6, end - last_stat_t)
    write_stats(end, (sent_bytes - last_stat_bytes) * 8.0 / dt)
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
