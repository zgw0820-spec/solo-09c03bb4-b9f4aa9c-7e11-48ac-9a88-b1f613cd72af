#!/usr/bin/env python3
"""Standalone paced UDP test sender (run inside lab-src netns).

Self-contained on purpose: it is launched by path via
``ip netns exec ... python udp_send.py`` so package imports are avoided.

Datagram layout: ``!4sBQd`` header + zero padding to --size bytes.
Writes a small JSON stats file once per second.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dst-ip", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--flow", required=True)
    ap.add_argument("--flow-id", type=int, default=1)
    ap.add_argument("--rate", default="12mbit", help="offered rate, 0=flood")
    ap.add_argument("--size", type=int, default=1200)
    ap.add_argument("--stats-file", required=True)
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
