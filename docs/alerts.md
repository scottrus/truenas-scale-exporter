# Alerting rules

> **The rules themselves live in [`rules/truenas-alerts.yaml`](../rules/truenas-alerts.yaml)**,
> which is a loadable Prometheus rule file and works unchanged as the
> `spec.groups` of a `PrometheusRule` or a VictoriaMetrics `VMRule`. It is unit
> tested by [`tests/rules/`](../tests/rules/) via `make rules`.
>
> This page is the reasoning behind them — why each rule is shaped the way it
> is, and what it costs to get it wrong. Load the YAML; read this to understand
> it.

Thresholds like scrub age and temperature are site-specific — look at them
before adopting the file verbatim.

The ordering is deliberate: the first three catch failures that a naive "is the
pool ONLINE" check misses entirely.

---

## 1. The pool is not healthy

The single most useful rule. `truenas_pool_healthy` folds TrueNAS's own
judgement into one number, and it goes to 0 for degraded, faulted and suspended
pools alike.

```yaml
- alert: TrueNASPoolUnhealthy
  expr: truenas_pool_healthy == 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "ZFS pool {{ $labels.pool }} is unhealthy"
    description: >-
      Check `zpool status {{ $labels.pool }}`. See the device error counters
      for which member is at fault.
```

## 2. The pool has suspended

A suspended pool has stopped serving I/O entirely. Anything with a volume on it
is already failing, and every dataset served from it is effectively gone until
it is recovered.

```yaml
- alert: TrueNASPoolSuspended
  expr: truenas_pool_status_code{status_code="IO_FAILURE_CONTINUE"} == 1
  for: 1m
  labels:
    severity: critical
  annotations:
    summary: "ZFS pool {{ $labels.pool }} has SUSPENDED"
    description: >-
      All I/O to this pool is failing. Per-device status can still read ONLINE
      in this state, so do not treat device status as reassurance.
```

## 3. The pool has lost redundancy while still reporting ONLINE

**This is the rule most setups are missing.**

When a mirror leg is detached — during an RMA, say — the vdev stops being a
`MIRROR` and becomes a bare `DISK`. The pool reports `ONLINE` with full
confidence. There is no degraded state, no warning, and any alert keyed on pool
status stays green for the entire window in which a single further failure
means total loss. Worse, with no second copy there is no self-healing: a
checksum error becomes permanent data loss rather than something ZFS repairs.

```yaml
# A mirror that is down to one leg.
- alert: TrueNASMirrorDegraded
  expr: truenas_pool_vdev_children{vdev_type="MIRROR"} < 2
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "Mirror {{ $labels.vdev }} in pool {{ $labels.pool }} has no redundancy"

# A data vdev with no redundancy at all — the post-detach shape above.
# Exclude any pool you intentionally run as a stripe.
- alert: TrueNASPoolHasNoRedundancy
  expr: |
    truenas_pool_vdev_status{category="data", vdev_type="DISK"} == 1
    unless on (pool) truenas_pool_vdev_status{pool=~"scratch|tmp"}
  for: 15m
  labels:
    severity: critical
  annotations:
    summary: "Pool {{ $labels.pool }} has a data vdev with no redundancy"
    description: >-
      Vdev {{ $labels.vdev }} is a bare disk. A single failure loses the pool,
      and scrubs can only detect corruption, not repair it.
```

## 4. Devices are accumulating errors

ZFS error counters are the earliest reliable signal of a failing device — they
move well before a pool changes state, and they moved during incidents where
SMART stayed completely clean.

```yaml
- alert: TrueNASDeviceErrors
  expr: |
    increase(truenas_pool_device_read_errors_total[1h]) > 0
    or increase(truenas_pool_device_write_errors_total[1h]) > 0
    or increase(truenas_pool_device_checksum_errors_total[1h]) > 0
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Device {{ $labels.device }} in {{ $labels.pool }} is logging ZFS errors"
```

Checksum errors on their own deserve attention even at low rates — they mean
data came back wrong, not just slowly.

## 5. A device has left the pool

```yaml
- alert: TrueNASDeviceNotOnline
  expr: truenas_pool_device_online == 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "Device {{ $labels.device }} in {{ $labels.pool }} is not ONLINE"
```

## 6. A drive has stopped answering

When a controller wedges, the drive stops responding to admin commands. The
temperature reading is the tell: it flatlines or drops to zero while the drive
is still nominally present.

```yaml
- alert: TrueNASDiskTemperatureImplausible
  expr: truenas_disk_temperature_celsius == 0
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Disk {{ $labels.disk }} ({{ $labels.serial }}) reports 0 °C"
    description: >-
      A zero reading usually means the drive stopped answering admin commands
      rather than that it is genuinely cold.

- alert: TrueNASDiskTemperatureHigh
  expr: truenas_disk_temperature_celsius > 55
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "Disk {{ $labels.disk }} at {{ $value }} °C"
```

Adjust 55 °C to your drives. Spinning disks and NVMe have very different
sensible ceilings; NVMe frequently idles above 50 °C without concern.

## 7. Scrubs are not running

A pool that is never scrubbed is a pool whose corruption you have not found yet.

**Filter to `function="SCRUB"`.** ZFS reports only the most recent scan per
pool, and a resilver is a scan. A pool that resilvered an hour ago therefore
looks freshly scanned — but a resilver only verifies the data it rebuilt, not
the whole pool. Aggregating across scan types lets a resilver mask a pool that
has never been scrubbed, which is precisely the pool you most want scrubbed:
one that just had a disk replaced.

```yaml
- alert: TrueNASScrubTooOld
  expr: |
    time() - max by (pool) (
      truenas_pool_scan_end_timestamp_seconds{function="SCRUB"}
    ) > 40 * 24 * 3600
  labels:
    severity: warning
  annotations:
    summary: "Pool {{ $labels.pool }} has not been scrubbed in over 40 days"

# A pool with no SCRUB record at all never matches the rule above — there is no
# series for the comparison to evaluate. This catches that case.
- alert: TrueNASPoolNeverScrubbed
  expr: |
    truenas_pool_healthy
    unless on (pool) truenas_pool_scan_end_timestamp_seconds{function="SCRUB"}
  for: 1h
  labels:
    severity: warning
  annotations:
    summary: "Pool {{ $labels.pool }} has no completed scrub on record"

- alert: TrueNASScrubErrors
  expr: truenas_pool_scan_errors > 0
  labels:
    severity: critical
  annotations:
    summary: "{{ $labels.function }} of {{ $labels.pool }} found {{ $value }} errors"
```

TrueNAS's default scrub interval is 35 days, so 40 gives one cycle of slack
before the alert fires on a schedule that is merely late rather than broken.

## 8. An SSD pool is not being trimmed

An untrimmed SSD pool degrades the drive's flash translation layer over time.
It presents as escalating latency and, eventually, as what looks convincingly
like drive failure — a failure mode that has cost people a wrongly-returned
drive.

```yaml
- alert: TrueNASPoolAutotrimDisabled
  expr: truenas_pool_autotrim_enabled == 0
  labels:
    severity: info
  annotations:
    summary: "Pool {{ $labels.pool }} has autotrim disabled"
    description: >-
      Harmless on spinning disks — silence those pools. On an SSD or NVMe pool,
      enable it with `zpool set autotrim=on {{ $labels.pool }}`.
```

## 9. Capacity

ZFS performance degrades sharply as a pool fills, and fragmentation rises with
it.

```yaml
- alert: TrueNASPoolNearlyFull
  expr: |
    truenas_pool_allocated_bytes / truenas_pool_size_bytes > 0.80
  for: 1h
  labels:
    severity: warning
  annotations:
    summary: "Pool {{ $labels.pool }} is {{ $value | humanizePercentage }} full"
```

## 10. TrueNAS's own alerts, including SMART

25.10 polls SMART inside the middleware every 90 minutes and surfaces the result
only as an alert — there is no attribute API left to scrape. So SMART reaches
Prometheus through `truenas_alerts`.

```yaml
- alert: TrueNASCriticalAlert
  expr: truenas_alerts{level="CRITICAL", dismissed="false"} > 0
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "TrueNAS has {{ $value }} critical {{ $labels.klass }} alert(s)"
```

`dismissed="false"` matters: TrueNAS keeps dismissed alerts in the list with
their occurrence counter still updating, so without the filter an alert you
consciously acknowledged will keep firing.

## 11. The exporter itself

Never trust a monitoring signal you are not monitoring.

```yaml
- alert: TrueNASExporterDown
  expr: up{job=~".*truenas.*"} == 0
  for: 10m
  labels:
    severity: warning

- alert: TrueNASCollectorFailing
  expr: truenas_collector_success == 0
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "The {{ $labels.collector }} collector is failing"
    description: >-
      Other collectors may still be reporting, so metrics look healthy while
      one part of the picture is missing.
```

---

## A note on where the exporter runs

If you run the exporter on the TrueNAS appliance itself, every rule here fails
silently the moment the thing you care about goes wrong: the pool suspends, the
exporter goes down with it, and you get an exporter-down alert instead of a
pool-suspended one — assuming anything is still alive to notice.

Run it in a different failure domain, and keep at least one heartbeat that does
not depend on the storage you are monitoring.
