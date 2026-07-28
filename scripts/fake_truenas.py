#!/usr/bin/env python3
"""A fake TrueNAS middleware, for trying the exporter without an appliance.

Serves the six read-only JSON-RPC methods the exporter calls, with response
shapes copied from a real TrueNAS 25.10.5. Useful for:

  * seeing real exposition output before you have an API key
  * developing dashboards and alert rules against interesting states
  * contributing without owning a NAS

The sample topology is deliberately not all-healthy. `fast` is a mirror that has
lost a leg and is now a bare DISK vdev — it reports ONLINE with `healthy: true`
while having no redundancy at all. That is the case pool-status monitoring
misses, and it is the one worth having in front of you when writing rules.

    python scripts/fake_truenas.py &
    truenas-scale-exporter --url ws://127.0.0.1:8765 --api-key anything --once
"""

from __future__ import annotations

import argparse
import json
import logging
import time

from websockets.sync.server import serve

log = logging.getLogger("fake-truenas")

NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000


def pools():
    return [
        # A healthy RAIDZ2 — the ordinary case.
        {
            "name": "tank",
            "status": "ONLINE",
            "status_code": "OK",
            "healthy": True,
            "warning": False,
            "size": 159944582103040,
            "allocated": 60185738293248,
            "free": 99758843809792,
            "fragmentation": "21",
            "autotrim": {"value": "off"},
            "scan": {
                "function": "SCRUB",
                "state": "FINISHED",
                "start_time": {"$date": NOW_MS - (9 * DAY_MS)},
                "end_time": {"$date": NOW_MS - (9 * DAY_MS) + 35_000_000},
                "errors": 0,
                "percentage": 100.0,
            },
            "topology": {
                "data": [
                    {
                        "name": "raidz2-0",
                        "type": "RAIDZ2",
                        "status": "ONLINE",
                        "children": [
                            {
                                "disk": f"sd{c}",
                                "device": f"sd{c}2",
                                "status": "ONLINE",
                                "stats": {
                                    "read_errors": 0,
                                    "write_errors": 0,
                                    "checksum_errors": 0,
                                },
                            }
                            for c in "abcdefgh"
                        ],
                    }
                ],
                "log": [],
                "cache": [],
                "spare": [],
                "special": [],
                "dedup": [],
            },
        },
        # THE INTERESTING ONE. A mirror whose second leg was detached. ZFS
        # reports ONLINE and healthy; the vdev is now a bare DISK with one
        # child and a pile of read/write errors. Nothing about the pool status
        # tells you redundancy is gone.
        {
            "name": "fast",
            "status": "ONLINE",
            "status_code": "OK",
            "healthy": True,
            "warning": False,
            "size": 1992864825344,
            "allocated": 7197945856,
            "free": 1985666879488,
            "fragmentation": "4",
            "autotrim": {"value": "off"},
            "scan": {
                "function": "RESILVER",
                "state": "FINISHED",
                "start_time": {"$date": NOW_MS - 7_200_000},
                "end_time": {"$date": NOW_MS - 7_190_000},
                "errors": 0,
                "percentage": 100.0,
            },
            "topology": {
                "data": [
                    {
                        "name": "nvme3n1p2",
                        "type": "DISK",
                        "status": "ONLINE",
                        "disk": "nvme3n1",
                        "children": [],
                        "stats": {
                            "read_errors": 250,
                            "write_errors": 30,
                            "checksum_errors": 0,
                        },
                    }
                ],
                "log": [],
                "cache": [],
                "spare": [],
                "special": [],
                "dedup": [],
            },
        },
        # A pool that has genuinely degraded — one leg REMOVED.
        {
            "name": "scratch",
            "status": "DEGRADED",
            "status_code": "FAULTED_DEV",
            "healthy": False,
            "warning": True,
            "size": 493921239040,
            "allocated": 400000000000,
            "free": 93921239040,
            "fragmentation": "6",
            "autotrim": {"value": "on"},
            "scan": {
                "function": "SCRUB",
                "state": "FINISHED",
                "start_time": {"$date": NOW_MS - (55 * DAY_MS)},
                "end_time": {"$date": NOW_MS - (55 * DAY_MS) + 15_000},
                "errors": 2,
                "percentage": 100.0,
            },
            "topology": {
                "data": [
                    {
                        "name": "mirror-0",
                        "type": "MIRROR",
                        "status": "DEGRADED",
                        "children": [
                            {
                                "disk": "sdp",
                                "status": "ONLINE",
                                "stats": {
                                    "read_errors": 0,
                                    "write_errors": 0,
                                    "checksum_errors": 4,
                                },
                            },
                            {
                                "disk": "sdr",
                                "status": "REMOVED",
                                "stats": {
                                    "read_errors": 12,
                                    "write_errors": 3,
                                    "checksum_errors": 0,
                                },
                            },
                        ],
                    }
                ],
                "log": [],
                "cache": [],
                "spare": [],
                "special": [],
                "dedup": [],
            },
        },
    ]


def disks():
    inventory = [
        ("nvme3n1", "EXAMPLE NVME 2TB", "NVME000001", "SSD"),
        ("sdp", "EXAMPLE SSD 500G", "SSD000001", "SSD"),
        ("sdr", "EXAMPLE SSD 500G", "SSD000002", "SSD"),
    ]
    inventory += [
        (f"sd{c}", "EXAMPLE HDD 10T", f"HDD{i:06d}", "HDD")
        for i, c in enumerate("abcdefgh", start=1)
    ]
    return [{"name": n, "model": m, "serial": s, "type": t} for n, m, s, t in inventory]


def temperatures():
    temps = {"nvme3n1": 41, "sdp": 30, "sdr": 0}  # sdr reads 0 — it is REMOVED
    temps.update({f"sd{c}": 37 + i for i, c in enumerate("abcdefgh")})
    return temps


def alerts():
    return [
        {
            "klass": "SMARTStat",
            "level": "CRITICAL",
            "dismissed": False,
            "formatted": "Device sdr is failing SMART attribute Reallocated_Sector_Ct.",
        },
        {
            "klass": "VolumeStatus",
            "level": "CRITICAL",
            "dismissed": False,
            "formatted": "Pool scratch state is DEGRADED.",
        },
        {
            "klass": "ScrubFinished",
            "level": "INFO",
            "dismissed": True,
            "formatted": "Scrub of pool tank finished.",
        },
    ]


HANDLERS = {
    "auth.login_with_api_key": lambda: True,
    "pool.query": pools,
    "disk.query": disks,
    "disk.temperatures": temperatures,
    "alert.list": alerts,
    "system.info": lambda: {
        "version": "25.10.5",
        "hostname": "fake-truenas",
        "model": "Example Xeon",
        "system_product": "Fake Server",
        "uptime_seconds": 24446.8,
        "boottime": {"$date": NOW_MS - 24_446_800},
        "physmem": 134709493760,
    },
}


def handle(websocket):
    log.info("client connected")
    for raw in websocket:
        request = json.loads(raw)
        method = request.get("method", "")
        log.info("-> %s", method)

        if method not in HANDLERS:
            # Mirror the middleware's error envelope for an unknown method, so
            # the exporter's error handling is exercised too.
            websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "error": {
                            "code": -32601,
                            "message": f"Unknown method {method}",
                        },
                    }
                )
            )
            continue

        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": HANDLERS[method](),
                }
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    with serve(handle, args.host, args.port) as server:
        log.info("fake TrueNAS on ws://%s:%s — Ctrl-C to stop", args.host, args.port)
        log.info(
            "pools: tank (healthy), fast (ONLINE, no redundancy), scratch (DEGRADED)"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
