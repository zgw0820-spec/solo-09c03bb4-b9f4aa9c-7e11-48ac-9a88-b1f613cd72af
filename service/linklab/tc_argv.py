"""Topology description and pure ``tc``/``ip`` argv builders.

Topology (created by ``setup_lab.py`` inside the privileged container)::

    netns lab-src                         netns lab-dst
    +--------------------------+          +-------------------------+
    | uplink 10.77.0.1/24      |  veth    | downlink 10.77.0.2/24   |
    |   root qdisc 1: (htb)    |<-------->|                         |
    |   1:1 total root class   |  pair    | UDP receivers :5001    |
    |    1:10 alarm rate/ceil  |          |               :5002    |
    |    1:20 video rate/ceil  |          +-------------------------+
    +--------------------------+

Shaping happens on egress of ``uplink`` in ``lab-src``, i.e. exactly the
park uplink direction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .model import CLASS_KEYS, LabConfig
from .planner import Operation
from .units import kbps_to_tc

# UDP destination ports used by the two flows.
FLOW_PORT = {"alarm": 5001, "video": 5002}
FLOW_DSCP = {"alarm": 17, "video": 35}  # AF21 / AF41-ish markers (informational)


@dataclass(frozen=True)
class Topology:
    src_netns: str | None = "lab-src"
    dst_netns: str | None = "lab-dst"
    dev: str = "uplink"
    dst_dev: str = "downlink"
    src_ip: str = "10.77.0.1"
    dst_ip: str = "10.77.0.2"
    prefix_len: int = 24
    root_handle: str = "1:"
    root_classid: str = "1:1"
    classid: dict[str, str] = field(
        default_factory=lambda: {"alarm": "1:10", "video": "1:20"}
    )

    @staticmethod
    def from_env() -> "Topology":
        def ns(name: str, default: str | None) -> str | None:
            v = os.environ.get(name, default if default is not None else "")
            v = v.strip()
            return v or None

        return Topology(
            src_netns=ns("LAB_SRC_NS", "lab-src"),
            dst_netns=ns("LAB_DST_NS", "lab-dst"),
            dev=os.environ.get("LAB_DEV", "uplink"),
            dst_dev=os.environ.get("LAB_DST_DEV", "downlink"),
            src_ip=os.environ.get("LAB_SRC_IP", "10.77.0.1"),
            dst_ip=os.environ.get("LAB_DST_IP", "10.77.0.2"),
        )

    def src_wrap(self, argv: list[str]) -> list[str]:
        if self.src_netns:
            return ["ip", "netns", "exec", self.src_netns] + argv
        return list(argv)

    def dst_wrap(self, argv: list[str]) -> list[str]:
        if self.dst_netns:
            return ["ip", "netns", "exec", self.dst_netns] + argv
        return list(argv)


# ---------------------------------------------------------------------------
# Runtime change commands (online, in-place: `tc class change` only).
# ---------------------------------------------------------------------------


def argv_for_operation(op: Operation, topo: Topology) -> list[str]:
    """Map a planner operation to a real tc argv (with netns prefix)."""
    if op.target == "root":
        argv = [
            "tc",
            "class",
            "change",
            "dev",
            topo.dev,
            "classid",
            topo.root_classid,
            "htb",
            "rate",
            kbps_to_tc(op.rate_kbps),
            "ceil",
            kbps_to_tc(op.ceil_kbps),
        ]
    elif op.target in CLASS_KEYS:
        argv = [
            "tc",
            "class",
            "change",
            "dev",
            topo.dev,
            "classid",
            topo.classid[op.target],
            "htb",
            "rate",
            kbps_to_tc(op.rate_kbps),
            "ceil",
            kbps_to_tc(op.ceil_kbps),
        ]
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown operation target {op.target!r}")
    return topo.src_wrap(argv)


# ---------------------------------------------------------------------------
# One-shot lab setup commands.
# ---------------------------------------------------------------------------


def setup_argv(topo: Topology, cfg: LabConfig) -> list[tuple[str, list[str]]]:
    """All commands needed to (re)create the isolated link and HTB tree.

    Each item is ``(human_label, argv)``.  Netns creation itself must run in
    the root namespace, hence some commands are *not* src-wrapped.
    """
    cmds: list[tuple[str, list[str]]] = []
    src = topo.src_netns
    dst = topo.dst_netns

    # Tear down stale namespaces (deleting a ns destroys its veth ends).
    if src:
        cmds.append(("delete old src netns", ["ip", "netns", "del", src]))
    if dst:
        cmds.append(("delete old dst netns", ["ip", "netns", "del", dst]))

    if src:
        cmds.append(("create src netns", ["ip", "netns", "add", src]))
    if dst:
        cmds.append(("create dst netns", ["ip", "netns", "add", dst]))

    cmds.append(
        (
            "add veth pair",
            [
                "ip",
                "link",
                "add",
                "veth-s",
                "type",
                "veth",
                "peer",
                "name",
                "veth-d",
            ],
        )
    )
    if src:
        cmds.append(
            ("move src veth into netns", ["ip", "link", "set", "veth-s", "netns", src])
        )
    if dst:
        cmds.append(
            ("move dst veth into netns", ["ip", "link", "set", "veth-d", "netns", dst])
        )

    def in_src(argv: list[str]) -> list[str]:
        return topo.src_wrap(argv)

    def in_dst(argv: list[str]) -> list[str]:
        return topo.dst_wrap(argv)

    cmds += [
        (
            "rename src iface",
            in_src(["ip", "link", "set", "veth-s", "name", topo.dev]),
        ),
        (
            "rename dst iface",
            in_dst(["ip", "link", "set", "veth-d", "name", topo.dst_dev]),
        ),
        ("src loopback up", in_src(["ip", "link", "set", "lo", "up"])),
        ("dst loopback up", in_dst(["ip", "link", "set", "lo", "up"])),
        ("src iface up", in_src(["ip", "link", "set", topo.dev, "up", "mtu", "1500"])),
        ("dst iface up", in_dst(["ip", "link", "set", topo.dst_dev, "up"])),
        (
            "src address",
            in_src(
                [
                    "ip",
                    "addr",
                    "add",
                    f"{topo.src_ip}/{topo.prefix_len}",
                    "dev",
                    topo.dev,
                ]
            ),
        ),
        (
            "dst address",
            in_dst(
                [
                    "ip",
                    "addr",
                    "add",
                    f"{topo.dst_ip}/{topo.prefix_len}",
                    "dev",
                    topo.dst_dev,
                ]
            ),
        ),
    ]

    # ---- HTB qdisc + classes + filters -------------------------------
    cmds.append(
        (
            "add HTB root qdisc",
            in_src(
                [
                    "tc",
                    "qdisc",
                    "add",
                    "dev",
                    topo.dev,
                    "root",
                    "handle",
                    topo.root_handle,
                    "htb",
                    "default",
                    "20",
                ]
            ),
        )
    )
    cmds.append(
        (
            "add total root class 1:1",
            in_src(
                [
                    "tc",
                    "class",
                    "add",
                    "dev",
                    topo.dev,
                    "parent",
                    topo.root_handle,
                    "classid",
                    topo.root_classid,
                    "htb",
                    "rate",
                    kbps_to_tc(cfg.total_kbps),
                    "ceil",
                    kbps_to_tc(cfg.total_kbps),
                ]
            ),
        )
    )
    for key in CLASS_KEYS:
        c = cfg.class_cfg(key)
        cmds.append(
            (
                f"add {key} class {topo.classid[key]}",
                in_src(
                    [
                        "tc",
                        "class",
                        "add",
                        "dev",
                        topo.dev,
                        "parent",
                        topo.root_classid,
                        "classid",
                        topo.classid[key],
                        "htb",
                        "rate",
                        kbps_to_tc(c.rate_kbps),
                        "ceil",
                        kbps_to_tc(c.ceil_kbps),
                        "prio",
                        "1" if key == "alarm" else "2",
                    ]
                ),
            )
        )

    for key in CLASS_KEYS:
        port = FLOW_PORT[key]
        cmds.append(
            (
                f"classify udp/{port} -> {topo.classid[key]}",
                in_src(
                    [
                        "tc",
                        "filter",
                        "add",
                        "dev",
                        topo.dev,
                        "protocol",
                        "ip",
                        "parent",
                        topo.root_handle,
                        "prio",
                        "10" if key == "alarm" else "20",
                        "u32",
                        "match",
                        "ip",
                        "dport",
                        str(port),
                        "0xffff",
                        "flowid",
                        topo.classid[key],
                    ]
                ),
            )
        )

    return cmds


# ---------------------------------------------------------------------------
# Kernel inspection commands.
# ---------------------------------------------------------------------------


def kernel_report_argv(topo: Topology) -> list[tuple[str, list[str], bool]]:
    """``(section title, argv, wrap_in_src_netns)``."""
    return [
        (
            "tc qdisc (含统计)",
            ["tc", "-s", "-d", "qdisc", "show", "dev", topo.dev],
            True,
        ),
        (
            "tc class (HTB 类与实测令牌/发包统计)",
            ["tc", "-s", "-d", "class", "show", "dev", topo.dev],
            True,
        ),
        (
            "tc filter (UDP 端口分类规则)",
            ["tc", "-s", "filter", "show", "dev", topo.dev],
            True,
        ),
        (
            "链路状态 ip link",
            ["ip", "-d", "link", "show", topo.dev],
            True,
        ),
        ("网络命名空间", ["ip", "netns", "list"], False),
        (
            "连通性 ping (src -> dst)",
            topo.src_wrap(
                ["ping", "-c", "1", "-W", "1", topo.dst_ip]
            ),
            False,  # already wrapped
        ),
    ]
