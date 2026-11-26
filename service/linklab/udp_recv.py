#!/usr/bin/env python3
"""Standalone UDP receiver / measurer (run inside lab-dst netns).

Listens on one port per flow, tracks bytes/packets/sequence loss and
writes per-flow throughput samples to a JSON stats file every second.

DSCP / ECN verification
-----------------------
Each socket enables ``IP_RECVTOS`` and reads the *actually received* IPv4
TOS byte out of the recvmsg(2) ancillary data (no raw sockets / libpcap
needed).  A per-flow :class:`~linklab.dscp.MarkVerifier` splits received
packets into the old and the new segment at the sender-reported boundary
sequence number -- late old-segment packets therefore stay in the old
segment -- and keeps only fixed-size counters (bounded memory).  The new
segment is reported as "pending" until its first packet arrives.

The boundary plan is published per flow by the FlowManager at
``<run>/mark_plan_<flow>.json`` and re-read by the writer thread.

Self-contained (stdlib only, plus the sibling pure-logic dscp module).
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dscp import MarkVerifier  # noqa: E402

MAGIC = b"LL01"
HEADER = struct.Struct("!4sBQd")

_stop = threading.Event()


def _handle_signal(signum, frame):  # noqa: ANN001
    _stop.set()


class FlowStats:
    def __init__(self, name: str, port: int, plan_path: Path | None = None,
                 reorder_every: int = 0):
        self.name = name
        self.port = port
        self.plan_path = plan_path
        # Test-only: hold one packet back every N packets to emulate the
        # "old segment packet arrives late" path. 0 disables it.
        self.reorder_every = max(0, int(reorder_every))
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
        self.verifier = MarkVerifier(flow=name)
        # Test-only held packet ``(nbytes, seq, tos)`` used to emulate a
        # late old-segment arrival. None when nothing is held back.
        self._held: tuple[int, int | None, int | None] | None = None
        self._arrival_count = 0

    def _deliver(self, nbytes: int, seq: int | None, tos: int | None) -> None:
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
            self.verifier.record(tos, seq)

    def record(self, nbytes: int, seq: int | None, tos: int | None) -> None:
        # Optional reordering injection for the late-packet test: every Nth
        # packet is held back and released when the next one arrives, so the
        # held (older seq) packet is accounted for after the newer one.
        if self.reorder_every and seq is not None:
            self._arrival_count += 1
            if self._arrival_count % self.reorder_every == 0:
                held = self._held
                self._held = (nbytes, seq, tos)
                if held is not None:
                    self._deliver(*held)
                return
            if self._held is not None:
                held, self._held = self._held, None
                self._deliver(*held)
        self._deliver(nbytes, seq, tos)

    def flush_reorder(self) -> None:
        if self._held is not None:
            held, self._held = self._held, None
            self._deliver(*held)

    def refresh_plan(self) -> None:
        if self.plan_path is None:
            return
        with self.lock:
            if self.plan_path.exists():
                self.verifier.load_plan_file(self.plan_path)
            elif self.verifier.plan_request_id is not None:
                # The plan file was removed at the end of a trial: reset to
                # a single old-segment verifier for the next sending round.
                self.verifier = MarkVerifier(flow=self.name)

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
            marking = self.verifier.snapshot()
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
                "marking": marking,
            }


def _recv_one(sock: socket.socket):
    """Receive one datagram plus its TOS ancillary item.

    Returns ``(data, seq, tos)`` where ``tos`` is None when the platform
    did not provide IP_RECVTOS ancillary data.
    """
    try:
        data, ancdata, _flags, _addr = sock.recvmsg(65535, 256)
    except (BlockingIOError, InterruptedError):
        return None
    tos = None
    for level, typ, val in ancdata:
        # Linux reports the TOS ancillary item with cmsg_type IP_TOS even
        # though the socket option enabling it is IP_RECVTOS; accept both.
        if level == socket.IPPROTO_IP and typ in (
            socket.IP_TOS,
            getattr(socket, "IP_RECVTOS", socket.IP_TOS),
        ):
            if isinstance(val, (bytes, bytearray)):
                tos = val[0] if val else None
            else:
                tos = int(val)
            break
    seq = None
    if len(data) >= HEADER.size:
        magic, _fid, seq, _ts = HEADER.unpack(data[: HEADER.size])
        if magic != MAGIC:
            seq = None
    return data, seq, tos


def serve(name: str, port: int, out: dict[str, FlowStats],
          plan_path: Path | None, reorder_every: int) -> None:
    st = FlowStats(name, port, plan_path=plan_path, reorder_every=reorder_every)
    out[name] = st
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    try:
        # Ask the kernel to attach the real IPv4 TOS byte to every packet.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
    except OSError:
        # Older/locked-down platform: throughput still works, marking
        # verification reports unknown_tos instead of guessing.
        pass
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    while not _stop.is_set():
        try:
            got = _recv_one(sock)
        except socket.timeout:
            continue
        except OSError:
            break
        if got is None:
            continue
        data, seq, tos = got
        st.record(len(data), seq, tos)
    st.flush_reorder()
    sock.close()


def writer_loop(path: Path, flows: dict[str, FlowStats]) -> None:
    while not _stop.is_set():
        time.sleep(1.0)
        for st in flows.values():
            st.refresh_plan()
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
    ap.add_argument(
        "--plan-dir",
        default=None,
        help="directory read for per-flow mark_plan_<flow>.json files",
    )
    ap.add_argument(
        "--reorder-every",
        type=int,
        default=0,
        help="test-only: hold one packet every N to emulate late arrivals",
    )
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    specs = args.flow or ["alarm:5001", "video:5002"]
    plan_dir = Path(args.plan_dir) if args.plan_dir else None
    flows: dict[str, FlowStats] = {}
    threads = []
    for spec in specs:
        name, _, port_s = spec.partition(":")
        plan_path = plan_dir / f"mark_plan_{name}.json" if plan_dir else None
        t = threading.Thread(
            target=serve,
            args=(name, int(port_s), flows, plan_path, args.reorder_every),
            daemon=True,
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
    for st in flows.values():
        st.refresh_plan()
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
