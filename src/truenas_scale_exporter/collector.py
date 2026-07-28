"""Prometheus collectors for TrueNAS pool, disk and alert state.

Design notes worth knowing before changing anything here:

* **Pool status alone is not a health signal.** A mirror whose second leg has
  been detached reports ``ONLINE`` while having no redundancy left at all, and
  a pool that has suspended reports ``ONLINE`` per-device. So this exporter
  emits vdev child counts and per-device error counters alongside the status,
  because those are what actually move when a disk is failing.
* **SMART attributes are deliberately absent.** TrueNAS 25.10 moved SMART
  inside the middleware — it is polled every 90 minutes and surfaced only as
  alerts. There is no queryable attribute API to export, so SMART reaches you
  here through ``truenas_alerts`` instead.
* Counters use ``CounterMetricFamily`` even though ZFS resets them on
  ``zpool clear`` and on import; Prometheus-compatible backends handle that
  reset correctly, whereas a gauge would hide it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    InfoMetricFamily,
)

from .client import TrueNASClient, TrueNASError

log = logging.getLogger(__name__)

# The states ZFS can report for a pool. All of them are emitted for every pool
# so that `truenas_pool_status{status="ONLINE"} == 0` is a reliable alerting
# expression — with only the current state emitted, a vanished series and a
# healthy pool would look the same to a rule.
POOL_STATES = (
    "ONLINE",
    "DEGRADED",
    "FAULTED",
    "OFFLINE",
    "REMOVED",
    "UNAVAIL",
    "SUSPENDED",
)

# Every vdev category pool.query reports. `data` carries the actual storage;
# `log`/`cache`/`special`/`dedup`/`spare` are auxiliary but can still fail.
TOPOLOGY_CATEGORIES = ("data", "log", "cache", "spare", "special", "dedup")


def _epoch_seconds(value: Any) -> float | None:
    """Unwrap the middleware's ``{"$date": <milliseconds>}`` timestamps."""
    if isinstance(value, dict) and "$date" in value:
        millis = value["$date"]
        if isinstance(millis, (int, float)):
            return millis / 1000.0
    return None


def _number(value: Any) -> float | None:
    """Coerce a middleware field to a float.

    Several numeric fields arrive as strings (``fragmentation`` is ``"21"``),
    so this is not redundant with plain float().
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class TrueNASCollector:
    """Collect-on-scrape collector.

    Each sub-collector is isolated: a failure in one (say the alert API
    changing shape) still lets the others report, and is surfaced as
    ``truenas_collector_success{collector="..."} 0`` rather than an empty
    scrape that looks like the appliance is fine.
    """

    def __init__(self, client: TrueNASClient) -> None:
        self._client = client

    def collect(self):  # noqa: C901 - a flat sequence of metric emissions
        started = time.monotonic()
        up = 1
        successes: dict[str, int] = {}

        collectors = (
            ("pools", self._collect_pools),
            ("disks", self._collect_disks),
            ("alerts", self._collect_alerts),
            ("system", self._collect_system),
        )

        for name, fn in collectors:
            try:
                yield from fn()
                successes[name] = 1
            except TrueNASError as exc:
                log.warning("collector %s failed: %s", name, exc)
                successes[name] = 0
                up = 0
            except Exception:  # pragma: no cover - defensive
                log.exception("collector %s raised unexpectedly", name)
                successes[name] = 0
                up = 0

        success = GaugeMetricFamily(
            "truenas_collector_success",
            "Whether each sub-collector completed successfully (1) or not (0).",
            labels=["collector"],
        )
        for name, value in successes.items():
            success.add_metric([name], value)
        yield success

        yield GaugeMetricFamily(
            "truenas_up",
            "Whether the exporter could reach the TrueNAS middleware and all "
            "collectors succeeded.",
            value=up,
        )
        yield GaugeMetricFamily(
            "truenas_scrape_duration_seconds",
            "Wall-clock time taken to collect all TrueNAS metrics.",
            value=time.monotonic() - started,
        )

    # -- pools ---------------------------------------------------------------

    def _collect_pools(self) -> Iterable[Any]:
        pools = self._client.call("pool.query")

        status = GaugeMetricFamily(
            "truenas_pool_status",
            "Pool status, as a state set. Exactly one status per pool is 1.",
            labels=["pool", "status"],
        )
        healthy = GaugeMetricFamily(
            "truenas_pool_healthy",
            "Whether TrueNAS considers the pool healthy (1) or not (0).",
            labels=["pool"],
        )
        warning = GaugeMetricFamily(
            "truenas_pool_warning",
            "Whether the pool is flagged with a warning condition.",
            labels=["pool"],
        )
        status_code = GaugeMetricFamily(
            "truenas_pool_status_code",
            "Pool status code as a state set, e.g. OK or IO_FAILURE_CONTINUE. "
            "This distinguishes a suspended pool from a merely degraded one.",
            labels=["pool", "status_code"],
        )
        size = GaugeMetricFamily(
            "truenas_pool_size_bytes", "Total pool size.", labels=["pool"]
        )
        allocated = GaugeMetricFamily(
            "truenas_pool_allocated_bytes", "Allocated space.", labels=["pool"]
        )
        free = GaugeMetricFamily(
            "truenas_pool_free_bytes", "Free space.", labels=["pool"]
        )
        fragmentation = GaugeMetricFamily(
            "truenas_pool_fragmentation_ratio",
            "Pool fragmentation, 0-1.",
            labels=["pool"],
        )
        autotrim = GaugeMetricFamily(
            "truenas_pool_autotrim_enabled",
            "Whether autotrim is enabled. An untrimmed SSD pool degrades the "
            "drive's flash translation layer and can present as drive failure.",
            labels=["pool"],
        )

        scan_state = GaugeMetricFamily(
            "truenas_pool_scan_state",
            "State of the most recent scrub or resilver, as a state set.",
            labels=["pool", "function", "state"],
        )
        scan_end = GaugeMetricFamily(
            "truenas_pool_scan_end_timestamp_seconds",
            "Completion time of the most recent scrub or resilver. Subtract "
            "from time() to alert on scrub age.",
            labels=["pool", "function"],
        )
        scan_errors = GaugeMetricFamily(
            "truenas_pool_scan_errors",
            "Errors recorded by the most recent scrub or resilver.",
            labels=["pool", "function"],
        )
        scan_progress = GaugeMetricFamily(
            "truenas_pool_scan_progress_ratio",
            "Progress of an in-flight scrub or resilver, 0-1.",
            labels=["pool", "function"],
        )

        vdev_status = GaugeMetricFamily(
            "truenas_pool_vdev_status",
            "Current status of a vdev; the emitted status label is the live one.",
            labels=["pool", "vdev", "vdev_type", "category", "status"],
        )
        vdev_children = GaugeMetricFamily(
            "truenas_pool_vdev_children",
            "Number of devices in a vdev. A MIRROR that drops to 1 has lost "
            "all redundancy while the pool still reports ONLINE.",
            labels=["pool", "vdev", "vdev_type", "category"],
        )
        dev_status = GaugeMetricFamily(
            "truenas_pool_device_status",
            "Current status of a leaf device in a vdev.",
            labels=["pool", "vdev", "device", "status"],
        )
        dev_online = GaugeMetricFamily(
            "truenas_pool_device_online",
            "Whether a leaf device is ONLINE (1) or in any other state (0).",
            labels=["pool", "vdev", "device"],
        )

        dev_read = CounterMetricFamily(
            "truenas_pool_device_read_errors",
            "ZFS read errors on a leaf device.",
            labels=["pool", "vdev", "device"],
        )
        dev_write = CounterMetricFamily(
            "truenas_pool_device_write_errors",
            "ZFS write errors on a leaf device.",
            labels=["pool", "vdev", "device"],
        )
        dev_cksum = CounterMetricFamily(
            "truenas_pool_device_checksum_errors",
            "ZFS checksum errors on a leaf device.",
            labels=["pool", "vdev", "device"],
        )

        for pool in pools:
            name = pool.get("name")
            if not name:
                continue

            live_status = pool.get("status")
            for candidate in POOL_STATES:
                status.add_metric(
                    [name, candidate], 1 if candidate == live_status else 0
                )
            if live_status and live_status not in POOL_STATES:
                # Forward-compatible: a state we do not know about still shows up.
                status.add_metric([name, live_status], 1)

            healthy.add_metric([name], 1 if pool.get("healthy") else 0)
            warning.add_metric([name], 1 if pool.get("warning") else 0)
            if pool.get("status_code"):
                status_code.add_metric([name, pool["status_code"]], 1)

            for family, key in (
                (size, "size"),
                (allocated, "allocated"),
                (free, "free"),
            ):
                value = _number(pool.get(key))
                if value is not None:
                    family.add_metric([name], value)

            frag = _number(pool.get("fragmentation"))
            if frag is not None:
                fragmentation.add_metric([name], frag / 100.0)

            trim = pool.get("autotrim") or {}
            if isinstance(trim, dict) and trim.get("value") is not None:
                autotrim.add_metric([name], 1 if trim["value"] == "on" else 0)

            self._emit_scan(
                pool, name, scan_state, scan_end, scan_errors, scan_progress
            )
            self._emit_topology(
                pool,
                name,
                vdev_status,
                vdev_children,
                dev_status,
                dev_online,
                dev_read,
                dev_write,
                dev_cksum,
            )

        yield from (
            status,
            healthy,
            warning,
            status_code,
            size,
            allocated,
            free,
            fragmentation,
            autotrim,
            scan_state,
            scan_end,
            scan_errors,
            scan_progress,
            vdev_status,
            vdev_children,
            dev_status,
            dev_online,
            dev_read,
            dev_write,
            dev_cksum,
        )

    @staticmethod
    def _emit_scan(pool, name, state_f, end_f, errors_f, progress_f) -> None:
        scan = pool.get("scan")
        if not isinstance(scan, dict):
            return

        function = scan.get("function") or "UNKNOWN"
        live = scan.get("state")
        for candidate in ("SCANNING", "FINISHED", "CANCELED"):
            state_f.add_metric(
                [name, function, candidate], 1 if candidate == live else 0
            )

        end = _epoch_seconds(scan.get("end_time"))
        if end is not None:
            end_f.add_metric([name, function], end)

        errors = _number(scan.get("errors"))
        if errors is not None:
            errors_f.add_metric([name, function], errors)

        pct = _number(scan.get("percentage"))
        if pct is not None:
            progress_f.add_metric([name, function], pct / 100.0)

    def _emit_topology(
        self,
        pool,
        name,
        vdev_status,
        vdev_children,
        dev_status,
        dev_online,
        dev_read,
        dev_write,
        dev_cksum,
    ) -> None:
        topology = pool.get("topology") or {}
        for category in TOPOLOGY_CATEGORIES:
            for vdev in topology.get(category) or []:
                vdev_name = vdev.get("name") or vdev.get("guid") or "unknown"
                vdev_type = vdev.get("type") or "UNKNOWN"
                labels = [name, vdev_name, vdev_type, category]

                if vdev.get("status"):
                    vdev_status.add_metric(labels + [vdev["status"]], 1)

                # A leaf DISK at the top level of a category is its own vdev —
                # a bare stripe. Report it as one child so "lost redundancy"
                # rules see a consistent shape.
                children = vdev.get("children") or []
                is_leaf = vdev_type == "DISK" and not children
                vdev_children.add_metric(labels, 1 if is_leaf else len(children))

                leaves = [vdev] if is_leaf else children
                for leaf in leaves:
                    device = (
                        leaf.get("disk")
                        or leaf.get("device")
                        or leaf.get("guid")
                        or "unknown"
                    )
                    dev_labels = [name, vdev_name, device]

                    leaf_status = leaf.get("status")
                    if leaf_status:
                        dev_status.add_metric(dev_labels + [leaf_status], 1)
                    dev_online.add_metric(
                        dev_labels, 1 if leaf_status == "ONLINE" else 0
                    )

                    stats = leaf.get("stats") or {}
                    for family, key in (
                        (dev_read, "read_errors"),
                        (dev_write, "write_errors"),
                        (dev_cksum, "checksum_errors"),
                    ):
                        value = _number(stats.get(key))
                        if value is not None:
                            family.add_metric(dev_labels, value)

    # -- disks ---------------------------------------------------------------

    def _collect_disks(self) -> Iterable[Any]:
        # disk.query supplies the model/serial/type labels; disk.temperatures
        # supplies the reading. Serial is the label that matters operationally —
        # it is what a warranty claim is filed against.
        try:
            inventory = self._client.call(
                "disk.query", [[], {"select": ["name", "serial", "model", "type"]}]
            )
        except TrueNASError:
            inventory = []

        meta = {
            d.get("name"): (
                d.get("model") or "",
                d.get("serial") or "",
                d.get("type") or "",
            )
            for d in inventory
            if d.get("name")
        }

        # disk.temperatures takes a required list of disk names — unlike
        # include_thresholds, `name` carries no default in the middleware
        # schema. Feed it what disk.query found. If the inventory came back
        # empty, still make the call with an empty list rather than skipping:
        # some versions treat that as "all disks", and a wrong guess here
        # costs a metric rather than the whole scrape.
        temps = self._client.call("disk.temperatures", [sorted(meta)]) or {}

        temperature = GaugeMetricFamily(
            "truenas_disk_temperature_celsius",
            "Disk temperature. A reading that stops updating, or drops to "
            "zero, means the drive stopped answering admin commands.",
            labels=["disk", "model", "serial", "type"],
        )
        for disk, value in sorted(temps.items()):
            reading = _number(value)
            if reading is None:
                continue
            model, serial, disk_type = meta.get(disk, ("", "", ""))
            temperature.add_metric([disk, model, serial, disk_type], reading)

        yield temperature

    # -- alerts --------------------------------------------------------------

    def _collect_alerts(self) -> Iterable[Any]:
        alerts = self._client.call("alert.list") or []

        counts: dict[tuple[str, str, str], int] = {}
        for alert in alerts:
            key = (
                alert.get("klass") or "unknown",
                alert.get("level") or "unknown",
                "true" if alert.get("dismissed") else "false",
            )
            counts[key] = counts.get(key, 0) + 1

        family = GaugeMetricFamily(
            "truenas_alerts",
            "Active TrueNAS alerts by class and level. SMART results reach "
            "Prometheus through here: 25.10 polls SMART internally every 90 "
            "minutes and exposes it only as alerts, not as attributes.",
            labels=["klass", "level", "dismissed"],
        )
        for (klass, level, dismissed), count in sorted(counts.items()):
            family.add_metric([klass, level, dismissed], count)

        yield family

    # -- system --------------------------------------------------------------

    def _collect_system(self) -> Iterable[Any]:
        info = self._client.call("system.info") or {}

        yield InfoMetricFamily(
            "truenas_system",
            "TrueNAS system identity and version.",
            value={
                "version": str(info.get("version") or ""),
                "hostname": str(info.get("hostname") or ""),
                "model": str(info.get("model") or ""),
                "system_product": str(info.get("system_product") or ""),
            },
        )

        uptime = _number(info.get("uptime_seconds"))
        if uptime is not None:
            yield GaugeMetricFamily(
                "truenas_system_uptime_seconds",
                "Time since last boot.",
                value=uptime,
            )

        boot = _epoch_seconds(info.get("boottime"))
        if boot is not None:
            yield GaugeMetricFamily(
                "truenas_system_boot_timestamp_seconds",
                "Boot time as a Unix timestamp.",
                value=boot,
            )

        physmem = _number(info.get("physmem"))
        if physmem is not None:
            yield GaugeMetricFamily(
                "truenas_system_physical_memory_bytes",
                "Installed physical memory.",
                value=physmem,
            )
