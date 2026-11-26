#!/usr/bin/env python3
"""Standalone UDP receiver / measurer (run inside lab-dst netns).

Listens on one port per flow, tracks bytes/packets/sequence loss and
writes per-flow throughput samples to a JSON stats file every second.

Self-contained (stdlib only).
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
from collections import deque
from pathlib import Path

MAGIC = b"LL01"
HEADER = struct.Struct("!4sBQd")

_stop = threading.Event()


def _handle_signal(signum, frame):  # noqa: ANN001
    _stop.set()


class FlowStats:
    def __init__(self, name: str, port: int):
        self.name = name
        self.port = port
        self.lock = threading.Lock()
        self.bytes_total = 0
        self.packets_total = 0
        self.gaps_lost = 0
        self.duplicates = 0
        self.last_seq: int | None = None
        self.started = time.time()
        # (monotonic time, cumulative bytes)
        self.samples: deque[tuple[float, int]] = deque(maxlen=16)
        self.samples.append((time.monotonic(), 0))

    def record(self, nbytes: int, seq: int | None) -> None:
        with self.lock:
            self.bytes_total += nbytes
            self.packets_total += 1
            if seq is not None:
                if self.last_seq is None:
                    pass
                elif seq > self.last_seq + 1:
                    self.gaps_lost += seq - (self.last_seq + 1)
                elif seq <= self.last_seq:
                    self.duplicates += 1
                self.last_seq = seq if self.last_seq is None else max(self.last_seq, seq)

    def snapshot(self) -> dict:
        now_m = time.monotonic()
        now = time.time()
        with self.lock:
            self.samples.append((now_m, self.bytes_total))
            # keep ~4 seconds of samples
            while len(self.samples) > 2 and now_m - self.samples[0][0] > 4.0:
                self.samples.popleft()

            def rate_over(seconds: float) -> float:
                target_t = now_m - seconds
                old_t, old_b = self.samples[0]
                for t, b in self.samples:
                    if t <= target_t:
                        old_t, old_b = t, b
                    else:
                        break
                dt = now_m - old_t
                if dt <= 1e-6:
                    return 0.0
                return max(0, self.bytes_total - old_b) * 8.0 / dt

            rate_1s = rate_over(1.0)
            rate_3s = rate_over(3.0)
            return {
                "port": self.port,
                "running": True,
                "bytes_total": self.bytes_total,
                "packets_total": self.packets_total,
                "rate_bps": int(rate_1s),
                "rate_mbps": round(rate_1s / 1e6, 3),
                "rate_3s_mbps": round(rate_3s / 1e6, 3),
                "seq_loss": self.gaps_lost,
                "duplicates": self.duplicates,
                "last_seq": self.last_seq,
                "age_s": round(now - self.started, 1),
            }


def serve(name: str, port: int, out: dict[str, FlowStats]) -> None:
    st = FlowStats(name, port)
    out[name] = st
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    while not _stop.is_set():
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        seq = None
        if len(data) >= HEADER.size:
            magic, _fid, seq, _ts = HEADER.unpack(data[: HEADER.size])
            if magic != MAGIC:
                seq = None
        st.record(len(data), seq)
    sock.close()


def writer_loop(path: Path, flows: dict[str, FlowStats]) -> None:
    while not _stop.is_set():
        time.sleep(1.0)
        data = {
            "updated_at": time.time(),
            "flows": {name: st.snapshot() for name, st in flows.items()},
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--flow",
        action="append",
        default=[],
        help="name:port, repeatable (default alarm:5001 video:5002)",
    )
    ap.add_argument("--stats-file", required=True)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    specs = args.flow or ["alarm:5001", "video:5002"]
    flows: dict[str, FlowStats] = {}
    threads = []
    for spec in specs:
        name, _, port_s = spec.partition(":")
        t = threading.Thread(
            target=serve, args=(name, int(port_s), flows), daemon=True
        )
        t.start()
        threads.append(t)

    wt = threading.Thread(
        target=writer_loop, args=(Path(args.stats_file), flows), daemon=True
    )
    wt.start()

    while not _stop.is_set():
        time.sleep(0.3)

    # Final stats flush.
    data = {
        "updated_at": time.time(),
        "stopped": True,
        "flows": {name: st.snapshot() for name, st in flows.items()},
    }
    try:
        Path(args.stats_file).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
