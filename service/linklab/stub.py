"""In-memory tc emulator for UI development and environments without
``CAP_NET_ADMIN``.

It understands just enough of our own argv vocabulary to keep a coherent
fake HTB tree and render ``tc -s class show``-style text.  It is *not* a
traffic shaper; the real Docker Compose deployment uses SubprocessRunner.
"""
from __future__ import annotations

import re
import time
from typing import Any

from .runner import CmdResult
from .tc_manager import FAULT_TOKEN


class StubTcRunner:
    def __init__(self, dev: str = "uplink"):
        self.dev = dev
        # classid -> (rate_kbps, ceil_kbps, parent)
        self.classes: dict[str, dict[str, Any]] = {
            "1:1": {"rate": 10_000, "ceil": 10_000, "parent": "1:"},
            "1:10": {"rate": 2_000, "ceil": 10_000, "parent": "1:1"},
            "1:20": {"rate": 6_000, "ceil": 10_000, "parent": "1:1"},
        }
        self.counters = {
            "1:10": {"bytes": 0, "packets": 0, "drops": 0, "overlimits": 0},
            "1:20": {"bytes": 0, "packets": 0, "drops": 0, "overlimits": 0},
        }
        self.started = time.time()

    # ------------------------------------------------------------------
    def run(self, argv: list[str]) -> CmdResult:
        argv = list(argv)
        if FAULT_TOKEN in argv:
            return CmdResult(
                argv=argv,
                rc=1,
                stderr=f'tc: error: unknown option "{FAULT_TOKEN}", aborting.',
            )
        # Strip `ip netns exec NS` prefix for parsing.
        if argv[:3] == ["ip", "netns", "exec"]:
            argv = argv[3:]
            if argv and argv[0] != "tc":  # different netns target etc.
                return CmdResult(argv=list(argv), rc=0, stdout="")

        if not argv:
            return CmdResult(argv=argv, rc=0)

        if argv[0] == "tc":
            return self._tc(argv)
        if argv[0] == "ip":
            if "netns" in argv and "list" in argv:
                return CmdResult(argv=argv, rc=0, stdout="lab-src (stub)\nlab-dst (stub)")
            return CmdResult(
                argv=argv,
                rc=0,
                stdout=(
                    f"4: {self.dev}: <BROADCAST,MULTICAST,UP,LOWER_UP> "
                    "mtu 1500 qdisc htb state UP\n"
                    "    link/ether 02:42:ac:11:00:02 brd ff:ff:ff:ff:ff:ff"
                ),
            )
        if argv[0] == "ping":
            return CmdResult(
                argv=argv,
                rc=0,
                stdout=(
                    "PING 10.77.0.2: 56 data bytes\n"
                    "64 bytes: icmp_seq=1 ttl=64 time=0.21 ms\n"
                    "--- ping statistics --- 2 packets transmitted, 2 received"
                ),
            )
        return CmdResult(argv=argv, rc=0)

    # ------------------------------------------------------------------
    def _tc(self, argv: list[str]) -> CmdResult:
        text = " ".join(argv)
        if "qdisc" in argv and "show" in argv:
            return CmdResult(
                argv=argv,
                rc=0,
                stdout=(
                    f"qdisc htb 1: dev {self.dev} root refcnt 2 r2q 10 "
                    "default 0x14 direct_packets_stat 0\n"
                    " Sent 123456 bytes 1024 pkt (dropped 0, overlimits 0)\n"
                ),
            )
        if "class" in argv and "show" in argv:
            return CmdResult(argv=argv, rc=0, stdout=self._render_classes())
        if "filter" in argv and "show" in argv:
            return CmdResult(
                argv=argv,
                rc=0,
                stdout=(
                    f"filter parent 1: protocol ip pref 10 u32\n"
                    f"filter parent 1: protocol ip pref 10 u32 fh 800: "
                    "ht type 1 order 256\n"
                    "  match ip dport 5001 0xffff\n"
                    "  flowid 1:10\n"
                    "filter parent 1: protocol ip pref 20 u32\n"
                    "  match ip dport 5002 0xffff\n"
                    "  flowid 1:20"
                ),
            )
        if "class" in argv and "change" in argv:
            return self._class_change(argv)
        if "qdisc" in argv and "add" in argv:
            return CmdResult(argv=argv, rc=0)
        if "class" in argv and "add" in argv:
            return CmdResult(argv=argv, rc=0)
        if "filter" in argv and "add" in argv:
            return CmdResult(argv=argv, rc=0)
        return CmdResult(argv=argv, rc=0, stdout=f"[stub] ok: {text}")

    def _class_change(self, argv: list[str]) -> CmdResult:
        cid = None
        rate = None
        ceil = None
        for i, a in enumerate(argv):
            if a == "classid" and i + 1 < len(argv):
                cid = argv[i + 1]
            if a == "rate" and i + 1 < len(argv):
                rate = self._to_kbps(argv[i + 1])
            if a == "ceil" and i + 1 < len(argv):
                ceil = self._to_kbps(argv[i + 1])
        if cid not in self.classes:
            return CmdResult(argv=argv, rc=2, stderr=f'RTNETLINK answers: No such file or directory (stub): class {cid}')
        if rate is not None:
            self.classes[cid]["rate"] = rate
        if ceil is not None:
            self.classes[cid]["ceil"] = ceil
        return CmdResult(argv=argv, rc=0)

    @staticmethod
    def _to_kbps(token: str) -> int:
        m = re.fullmatch(r"(\d+)(kbit|mbit|bps)?", token)
        if not m:
            return 0
        n = int(m.group(1))
        unit = m.group(2) or "bps"
        if unit == "mbit":
            return n * 1000
        if unit == "kbit":
            return n
        return n // 1000

    def _render_classes(self) -> str:
        lines = []
        for cid, c in self.classes.items():
            rate = c["rate"]
            ceil = c["ceil"]
            rate_h = f"{rate/1000:g}Mbit" if rate >= 1000 else f"{rate}Kbit"
            ceil_h = f"{ceil/1000:g}Mbit" if ceil >= 1000 else f"{ceil}Kbit"
            cnt = self.counters.get(cid, {"bytes": 0, "packets": 0, "drops": 0, "overlimits": 0})
            lines.append(
                f"class htb {cid} dev {self.dev} parent {c['parent']} prio 0 "
                f"rate {rate_h} ceil {ceil_h} burst 1500b cburst 1500b\n"
                f" Sent {cnt['bytes']} bytes {cnt['packets']} pkt "
                f"(dropped {cnt['drops']}, overlimits {cnt['overlimits']})"
            )
        lines.append("[stub 后端] 以上为内存模拟的 tc 输出，并非真实内核配置")
        return "\n".join(lines)
