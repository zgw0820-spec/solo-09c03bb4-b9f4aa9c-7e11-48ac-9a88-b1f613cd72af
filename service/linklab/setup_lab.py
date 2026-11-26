#!/usr/bin/env python3
"""Create the isolated veth/netns link and the initial HTB tree.

Runs once from ``entrypoint.sh`` in the privileged container.  Every
executed command is logged; "delete old netns" failures are expected on a
fresh host and are tolerated.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time

from .model import DEFAULT_CONFIG
from .runner import SubprocessRunner
from .tc_argv import Topology, setup_argv

# Failures of these best-effort cleanup commands are not fatal.
_BEST_EFFORT_PREFIXES = ("delete old",)


def main() -> int:
    if shutil.which("ip") is None or shutil.which("tc") is None:
        print(
            "[setup_lab] FATAL: ip/tc not found; install iproute2 in the image",
            file=sys.stderr,
        )
        return 127

    topo = Topology.from_env()
    cfg = DEFAULT_CONFIG
    runner = SubprocessRunner(timeout_s=20)

    # HTB kernel module auto-loads via netlink; try an explicit modprobe
    # too, but never fail if modprobe is absent/unavailable.
    for mod in ("sch_htb", "cls_u32", "act_mirred"):
        r = runner.run(["modprobe", mod])
        if r.ok:
            print(f"[setup_lab] modprobe {mod}: ok")

    for label, argv in setup_argv(topo, cfg):
        display = " ".join(argv)
        print(f"[setup_lab] + {label}: {display}", flush=True)
        res = runner.run(argv)
        if res.stdout:
            print(res.stdout)
        if not res.ok:
            if any(label.startswith(p) for p in _BEST_EFFORT_PREFIXES):
                print(f"[setup_lab]   (ignored rc={res.rc}: {res.stderr})")
                continue
            print(
                f"[setup_lab] FATAL while {label} (rc={res.rc}): {res.stderr}",
                file=sys.stderr,
            )
            return res.rc or 1

    # Brief settle + show the resulting tree for container logs.
    time.sleep(0.3)
    for title, argv, wrapped in [
        ("qdisc", ["tc", "qdisc", "show", "dev", topo.dev], True),
        ("class", ["tc", "class", "show", "dev", topo.dev], True),
    ]:
        real_argv = topo.src_wrap(argv) if wrapped else argv
        res = runner.run(real_argv)
        print(f"[setup_lab] {title}:\n{res.stdout or res.stderr}")

    print("[setup_lab] isolated link + HTB ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
