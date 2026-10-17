"""Abide, the Ravi scenario from the command line. Runs locally; no network, no Docker.

    python -m examples.delegate_demo.run                                  # all three arms
    python -m examples.delegate_demo.run --arm delegate --firewall off    # the gate alone (detector off)
"""

from __future__ import annotations

import argparse
import asyncio
import json

from intentseal_gateway.delegate_demo.scenario import Scenario, run, show


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["none", "ask-every-step", "delegate", "all"], default="all")
    ap.add_argument("--firewall", choices=["on", "off"], default="on")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    arms = ["none", "ask-every-step", "delegate"] if a.arm == "all" else [a.arm]
    for arm in arms:
        r = await run(Scenario("ravi-paint-quote"), arm, firewall=a.firewall == "on")
        print(json.dumps(r, indent=2, default=str)) if a.json else show(r)


if __name__ == "__main__":
    asyncio.run(main())
