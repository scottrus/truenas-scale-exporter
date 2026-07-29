# truenas-scale-exporter

A Prometheus exporter for TrueNAS that speaks the **JSON-RPC 2.0 over WebSocket API** —
the transport TrueNAS is migrating to — rather than the REST API that is removed in
TrueNAS 26.04 (Halfmoon).

It exports ZFS pool health, vdev topology, per-device error counters, scrub and resilver
state, disk temperatures, and TrueNAS's own alert stream.

---

## ⚠️ Read this before you deploy it

### This project was vibe-coded

It was written largely by an AI coding assistant, working against a real TrueNAS 25.10.5
appliance, with a human reviewing and directing. That is disclosed here so you can weigh it
honestly rather than discovering it from the commit history:

- The metric design is grounded in a real storage incident, not invented from documentation.
- It has unit tests built on real middleware response shapes, and it is dogfooded on the
  author's own cluster.
- It has **not** been battle-tested across many TrueNAS configurations, hardware layouts, or
  pool topologies. Multi-vdev RAIDZ, draid, dedup and special vdevs are handled generically
  and are less exercised than mirrors.

Read the source before trusting it — it is deliberately short and commented. Bug reports and
corrections from people running different configurations are genuinely wanted.

### Do not give it a privileged API key

**TrueNAS API keys inherit the privileges of the account that owns them.** A key created
under `root`, or under any full-admin account, grants total control of your NAS — pool
destruction, dataset deletion, share reconfiguration, user management. This exporter is a
network service. Do not hand it that.

Create a **dedicated user with the narrowest read-only role your TrueNAS version offers**
(a read-only admin role is sufficient), and issue the API key under that account.

The exporter only ever issues these six read calls, and never writes anything:

| Call | Used for |
|---|---|
| `auth.login_with_api_key` | authenticate the connection |
| `pool.query` | pool state, capacity, topology, scrub/resilver, device error counters |
| `boot.get_state` | the same, for `boot-pool`, which `pool.query` does not return |
| `disk.query` | disk model / serial / type, used only as metric labels |
| `disk.temperatures` | per-disk temperature |
| `alert.list` | TrueNAS's own alerts, including SMART |
| `system.info` | version, hostname, uptime |

If a future version needs anything beyond reads, that will be a major version bump and
called out in the changelog. Treat any build that asks for more privilege as suspect.

**Also:** the exporter serves `/metrics` unauthenticated, like most Prometheus exporters.
Disk serial numbers and pool names appear in labels. Keep it on a trusted network or put
your own authentication in front of it.

---

## Why this exists

Three things are true of TrueNAS 25.10 that together make most existing tooling a dead end:

1. **The REST API is removed in 26.04.** It was deprecated in 25.04, and 25.10 raises a
   `RESTAPIUsage` alert every time something authenticates against it. Exporters built on
   REST have a fixed expiry date. This one does not use it.

2. **SMART attributes are no longer queryable.** 25.10 moved SMART collection inside the
   middleware — it is polled every 90 minutes and surfaced *only as alerts*. There is no
   attribute API left to scrape. This exporter therefore exposes TrueNAS's alert stream
   (`truenas_alerts`) as the route to SMART, and leans on ZFS-level error counters for
   per-device health.

3. **The bundled netdata cannot fill the gap.** TrueNAS ships a stripped netdata with the
   ZFS collectors explicitly disabled (`/proc/spl/kstat/zfs/pool/state = no`). Its Graphite
   export carries pool *capacity* but no pool *state*, no scrub data, and no device error
   counters.

### The design lesson behind the metrics

A pool status of `ONLINE` is not a health signal.

When a mirror loses a leg and is detached, the pool reports `ONLINE` with full confidence —
while having no redundancy at all, and no self-healing, because a checksum error on a
single-device vdev is unrecoverable rather than repairable. Alerting on pool status alone
stays green through exactly the window in which you are most exposed.

So this exporter deliberately exports the things that *do* move:

- `truenas_pool_vdev_children` — a `MIRROR` that reads `1` has lost its redundancy.
- `truenas_pool_status_code` — distinguishes `IO_FAILURE_CONTINUE` (suspended) from `OK`.
- `truenas_pool_device_{read,write,checksum}_errors_total` — per-device, per-vdev.
- `truenas_pool_autotrim_enabled` — an untrimmed SSD pool degrades its flash translation
  layer and can present as drive failure.
- `truenas_disk_temperature_celsius` — a reading that stops updating or drops to zero means
  the drive stopped answering admin commands.

---

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `truenas_up` | gauge | — |
| `truenas_collector_success` | gauge | `collector` |
| `truenas_scrape_duration_seconds` | gauge | — |
| `truenas_pool_status` | gauge | `pool`, `status` |
| `truenas_pool_status_code` | gauge | `pool`, `status_code` |
| `truenas_pool_healthy` | gauge | `pool` |
| `truenas_pool_warning` | gauge | `pool` |
| `truenas_pool_size_bytes` | gauge | `pool` |
| `truenas_pool_allocated_bytes` | gauge | `pool` |
| `truenas_pool_free_bytes` | gauge | `pool` |
| `truenas_pool_fragmentation_ratio` | gauge | `pool` |
| `truenas_pool_autotrim_enabled` | gauge | `pool` |
| `truenas_pool_scan_state` | gauge | `pool`, `function`, `state` |
| `truenas_pool_scan_end_timestamp_seconds` | gauge | `pool`, `function` |
| `truenas_pool_scan_errors` | gauge | `pool`, `function` |
| `truenas_pool_scan_progress_ratio` | gauge | `pool`, `function` |
| `truenas_pool_vdev_status` | gauge | `pool`, `vdev`, `vdev_type`, `category`, `status` |
| `truenas_pool_vdev_children` | gauge | `pool`, `vdev`, `vdev_type`, `category` |
| `truenas_pool_device_status` | gauge | `pool`, `vdev`, `device`, `status` |
| `truenas_pool_device_online` | gauge | `pool`, `vdev`, `device` |
| `truenas_pool_device_read_errors_total` | counter | `pool`, `vdev`, `device` |
| `truenas_pool_device_write_errors_total` | counter | `pool`, `vdev`, `device` |
| `truenas_pool_device_checksum_errors_total` | counter | `pool`, `vdev`, `device` |
| `truenas_disk_temperature_celsius` | gauge | `disk`, `model`, `serial`, `type` |
| `truenas_alerts` | gauge | `klass`, `level`, `dismissed` |
| `truenas_system_info` | info | `version`, `hostname`, `model`, `system_product` |
| `truenas_system_uptime_seconds` | gauge | — |
| `truenas_system_boot_timestamp_seconds` | gauge | — |
| `truenas_system_physical_memory_bytes` | gauge | — |

`truenas_pool_status` emits **every** known ZFS state per pool, with the live one set to `1`
and the rest to `0`, so that `truenas_pool_status{status="ONLINE"} == 0` is a safe alerting
expression — a missing series and a healthy pool never look alike.

**`boot-pool` appears as an ordinary pool.** `pool.query` returns data pools only, so the
boot pool is collected separately through `boot.get_state` and emitted on the same
`truenas_pool_*` families — there is no `truenas_boot_pool_*`. Every rule and dashboard
panel keyed on `pool` therefore covers it for free. On a default single-device install that
means the no-redundancy rule fires against `boot-pool`, which is correct: the pool reads
`ONLINE` and healthy while a checksum error on it is permanent rather than repairable. If
that is a trade you have made deliberately, exclude the pool in your own rule or silence it
rather than dropping the metric.

The `device` label carries the **whole disk** (`sdz`), not the partition ZFS holds (`sdz3`),
so it joins with `truenas_disk_*`. `zpool status` shows the partition.

---

## Configuration

Every flag has an environment variable equivalent.

| Flag | Environment variable | Default | Notes |
|---|---|---|---|
| `--url` | `TRUENAS_URL` | — | Host or URL. `truenas.example.com` is enough. |
| `--api-key-file` | `TRUENAS_API_KEY_FILE` | — | **Preferred.** Keeps the key out of the environment. |
| `--api-key` | `TRUENAS_API_KEY` | — | Convenient, less safe. |
| `--listen-address` | `TRUENAS_EXPORTER_LISTEN_ADDRESS` | `0.0.0.0` | |
| `--port` | `TRUENAS_EXPORTER_PORT` | `9819` | Not in the Prometheus port registry; override if it clashes. |
| `--insecure` | `TRUENAS_INSECURE` | `false` | TrueNAS ships a self-signed certificate. |
| `--timeout` | `TRUENAS_TIMEOUT` | `15` | Per-call timeout, seconds. |
| `--log-level` | `TRUENAS_LOG_LEVEL` | `INFO` | |
| `--once` | — | — | Collect once, print, exit. Use this first. |

### Try it without a TrueNAS

There is a fake middleware in `scripts/` that serves the same six read-only
methods, with response shapes copied from a real 25.10.5. No appliance, no API
key, no Docker:

```bash
make demo          # collect once and print the exposition
make demo-serve    # serve /metrics on :9819 until Ctrl-C
```

The sample topology is deliberately not all-healthy. Pool `fast` is a mirror
that lost a leg and is now a bare `DISK` vdev: it reports `ONLINE` with
`healthy: true` while carrying 250 read and 30 write errors and having no
redundancy at all. That is the case pool-status monitoring misses, and it is
worth having in front of you when writing alert rules or building dashboards.

> **Kubernetes note.** The kubelet injects a legacy Docker-link variable for every
> Service in the namespace, so a Service named `truenas-exporter` sets
> `TRUENAS_EXPORTER_PORT=tcp://<clusterIP>:9819` — colliding with this exporter's own
> port setting. The chart sets `enableServiceLinks: false` to prevent it, and since
> 0.1.2 the binary ignores a non-numeric value rather than crashing. If you deploy from
> a raw manifest, set `enableServiceLinks: false` on the pod spec.

### Verify before you deploy

```bash
docker run --rm \
  -e TRUENAS_URL=truenas.example.com \
  -e TRUENAS_API_KEY=... \
  -e TRUENAS_INSECURE=true \
  ghcr.io/scottrus/truenas-scale-exporter:latest --once
```

This prints one full exposition and exits, so you find a bad key or a TLS problem before a
container is looping in the background.

---

## Deploying on Kubernetes

A Helm chart is published as an OCI artifact to the same registry as the image.

```bash
kubectl create secret generic truenas-exporter \
  --namespace monitoring \
  --from-literal=api-key='<read-only API key>'

helm install truenas-exporter \
  oci://ghcr.io/scottrus/charts/truenas-scale-exporter \
  --version 0.1.2 \
  --namespace monitoring \
  --set truenas.url=truenas.example.com \
  --set truenas.insecure=true \
  --set truenas.existingSecret=truenas-exporter
```

The chart runs the pod as non-root with a read-only root filesystem, all capabilities
dropped, and a `RuntimeDefault` seccomp profile — it satisfies the Pod Security `restricted`
profile as shipped.

### Scraping it

The chart can render a `ServiceMonitor` (Prometheus Operator) or a `VMServiceScrape`
(VictoriaMetrics Operator):

```yaml
serviceMonitor:
  enabled: true
  interval: 60s

# or, for the VictoriaMetrics operator
vmServiceScrape:
  enabled: true
  interval: 60s
```

A 60s interval is plenty. Pool state changes are not sub-minute events, and `disk.query`
plus `pool.query` are the two most expensive calls the middleware serves here.

---

## Deploying as a Docker container on Proxmox

If you are not running Kubernetes, run it on a Proxmox host (or any Docker host) and point
Prometheus at it as a static target.

```bash
mkdir -p /etc/truenas-exporter
printf '%s' '<read-only API key>' > /etc/truenas-exporter/api-key
chmod 600 /etc/truenas-exporter/api-key
```

```yaml
# /etc/truenas-exporter/compose.yaml
services:
  truenas-exporter:
    image: ghcr.io/scottrus/truenas-scale-exporter:0.1.2
    container_name: truenas-exporter
    restart: unless-stopped
    environment:
      TRUENAS_URL: truenas.example.com
      TRUENAS_API_KEY_FILE: /run/secrets/api-key
      TRUENAS_INSECURE: "true"
    volumes:
      - /etc/truenas-exporter/api-key:/run/secrets/api-key:ro
    ports:
      - "9819:9819"
    read_only: true
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
```

```bash
docker compose -f /etc/truenas-exporter/compose.yaml up -d
curl -s localhost:9819/metrics | head
```

Then scrape it:

```yaml
scrape_configs:
  - job_name: truenas
    scrape_interval: 60s
    static_configs:
      - targets: ["proxmox-host.example.com:9819"]
```

> **Do not run this container on the TrueNAS appliance itself.** If the pool it is monitoring
> suspends, the exporter goes down with it and you lose the signal exactly when you need it.
> Run it somewhere with an independent failure domain.

---

## Alerting

Rules that encode the lessons above are in [`docs/alerts.md`](docs/alerts.md), including the
`ONLINE`-but-not-redundant case that plain status monitoring misses. A Grafana dashboard is
in [`dashboards/`](dashboards/).

---

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Tests use canned middleware responses — no TrueNAS needed.

## Compatibility

| TrueNAS | Status |
|---|---|
| 25.04+ | Supported — versioned JSON-RPC endpoint at `/api/current` |
| 25.10.x | Developed and dogfooded against 25.10.5 |
| 26.04 | Expected to work; this is the release that removes REST |
| 24.10 and older | Unsupported — no versioned JSON-RPC endpoint |
| TrueNAS CORE | Unsupported |

An appliance that does not offer `boot.get_state` degrades rather than failing: `boot-pool`
is simply absent, `truenas_collector_success{collector="boot"}` reads `0`, and every other
collector reports normally. `truenas_up` deliberately ignores that collector, so a missing
boot API never masks otherwise healthy data pools.

## License

Apache-2.0. See [LICENSE](LICENSE).
