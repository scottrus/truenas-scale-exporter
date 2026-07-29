# Security

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/scottrus/truenas-scale-exporter/security/advisories/new)
rather than opening a public issue.

## The API key is the whole threat model

**TrueNAS API keys inherit the privileges of the account that owns them.** There
is no per-key scoping that limits what a key can do beyond what its user can do.
A key issued under `root`, or any full-admin account, grants complete control of
the NAS: destroying pools, deleting datasets, reconfiguring shares, adding
users.

This exporter is a long-running network service holding that key in memory. Give
it the least privilege that works:

1. Create a **dedicated user** for the exporter.
2. Give it the narrowest **read-only role** your TrueNAS version offers.
3. Issue the API key under that account.
4. Never reuse that key for anything else — a key used in two places cannot be
   revoked in one.

### What the exporter actually calls

Every middleware call it makes, in full:

| Call | Purpose |
|---|---|
| `auth.login_with_api_key` | authenticate the connection |
| `pool.query` | pool state, capacity, topology, scrub/resilver, device errors |
| `boot.get_state` | the same, for `boot-pool` — `pool.query` returns data pools only |
| `disk.query` | disk model / serial / type, used only as metric labels |
| `disk.temperatures` | per-disk temperature |
| `alert.list` | TrueNAS alerts, including SMART |
| `system.info` | version, hostname, uptime, memory |

All are reads. The exporter has no code path that writes.

This list is the exporter's privilege surface, and changes to it are announced
rather than slipped in:

- **A new read call** is a **minor** bump, called out under its own heading in
  the changelog, with this table updated in the same change. `boot.get_state`
  in 0.2.0 is the worked example.
- **Any call that writes** would be a **major** bump, and is not something this
  project intends to do — the exporter has no write code path today, and adding
  one changes what an API key here can cost you.

Treat any build asking for privileges beyond this table as suspect, and check
the diff.

## Handling the key

Prefer `--api-key-file` / `TRUENAS_API_KEY_FILE` over `--api-key` /
`TRUENAS_API_KEY`. A file keeps the key out of the process environment and out
of the process table, and is how both Kubernetes Secrets and Docker secrets
deliver it.

In the Helm chart, prefer `truenas.existingSecret` over `truenas.apiKey`. A key
passed as `truenas.apiKey` is stored in clear text in the Helm release secret
for as long as that revision is retained, in addition to the Secret the chart
creates.

The key is never logged. Log output at `DEBUG` includes the target URL and
method names, never parameters.

## The metrics endpoint is unauthenticated

Like most Prometheus exporters, `/metrics` has no authentication. It exposes
pool names, disk models and **disk serial numbers**. Keep it on a trusted
network, or put your own authentication in front of it.

## TLS

`--insecure` disables certificate verification, and is commonly needed because
TrueNAS ships a self-signed certificate. It leaves the connection vulnerable to
an active man-in-the-middle, who would obtain your API key. On a network where
that is a real concern, install a trusted certificate on TrueNAS and leave
verification on.

## Supply chain

- Images are built from Chainguard base images, pinned by digest.
- Every pull request scans the built image with [grype](https://github.com/anchore/grype),
  failing on any fixable HIGH or CRITICAL finding.
- Releases publish an SBOM and a signed build provenance attestation.
- Base image digests and dependencies are updated by Dependabot.

Because releases ship an SBOM, you can scan what was actually published without
pulling and unpacking the image — and without trusting our scan:

```bash
grype ghcr.io/scottrus/truenas-scale-exporter:<tag>
```

Verify a published image:

```bash
gh attestation verify \
  oci://ghcr.io/scottrus/truenas-scale-exporter:<tag> \
  --repo scottrus/truenas-scale-exporter
```
