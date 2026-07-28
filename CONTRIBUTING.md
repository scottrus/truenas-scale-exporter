# Contributing

Contributions are welcome, particularly from people running TrueNAS
configurations this has not seen — multi-vdev RAIDZ, draid, dedup and special
vdevs, and anything larger than a handful of disks.

## The workflow

`main` is protected. All changes land through a pull request.

1. Branch from `main`.
2. Make the change.
3. **Run `make check` locally.** This is the same gate the PR faces.
4. Open a PR.

## Run the checks before you push

Every check that runs in CI is defined in the `Makefile` and nowhere else — the
workflow calls the same targets. So this is not an approximation of CI, it is
CI:

```bash
make setup    # one-off: create .venv and install
make check    # lint, format, pylint, pytest, helm, docker
```

Individual gates, when you want a faster loop:

```bash
make lint     # ruff check, ruff format --check, pylint
make fmt      # apply formatting and autofixes
make test     # pytest
make actionlint      # workflow syntax, expressions, shellcheck on run: blocks
make actions-pinned  # every uses: is SHA-pinned with a version comment
make helm     # helm lint, template permutations, required values, kubeconform
make docker   # hadolint, image build, smoke test
make scan     # grype CVE scan (run make docker first)
```

Tools you do not have installed are reported as `SKIP` rather than failing, so
`make check` is useful on a laptop without Docker. CI sets `REQUIRE_ALL=1`,
which turns every skip into a failure — so a check that skipped locally will
still run against your PR.

Optional extras, if you want the full local gate:

```bash
brew install helm kubeconform hadolint grype actionlint
```

## What a good change looks like

- **Comments explain why, not what.** The existing code is commented at the
  points where a reader would otherwise wonder why something is the way it is.
  Match that, and skip narrating what the line plainly does.
- **New metrics need a test.** See below — no TrueNAS is needed to run the
  suite.
- **New metrics need a README row**, in the metrics table.
- **Read-only, always.** The exporter must never call a middleware method that
  writes. See [SECURITY.md](SECURITY.md).

## Tests

Nothing in the suite needs a TrueNAS appliance. It runs at two levels, and the
distinction is deliberate:

**`tests/test_collector.py` — canned middleware responses.** A `FakeClient`
returns recorded JSON per method name. The collector's job is dict → metrics, so
this is the right level for it: readable fixtures, no I/O, and a failure points
straight at the mapping.

**`tests/test_client.py` — a real websocket server on localhost.** The protocol
layer is *not* tested by patching `websockets.sync.client.connect`. Mocking the
library out would only prove this code agrees with our own assumptions about the
library, and those assumptions are the likeliest thing to be wrong — the
`ssl` / `ssl_context` keyword rename is exactly the bug a mock sails past. A
local server exercises the real handshake, framing and close semantics, so a
breaking change in `websockets` fails in CI rather than in someone's cluster. It
also lets reconnect-on-drop be tested by genuinely dropping the connection.

**What neither can prove** is that TrueNAS replies the way we assume. Only
`--once` against a real appliance does that. Green tests mean the code is
self-consistent, not that the assumptions are right.

So: **a scrubbed fixture from a real system is the most valuable thing you can
contribute**, especially for a topology this repo has not seen — multi-vdev
RAIDZ, draid, dedup or special vdevs. Strip pool names and serial numbers first.

Coverage has a floor of 80% (currently ~87%). It exists to catch a regression,
not to be chased; `main()`'s `serve_forever` loop is most of what is uncovered
and is not worth contorting the code to reach.

## Metric naming

Follow the [Prometheus naming conventions](https://prometheus.io/docs/practices/naming/):
`truenas_` prefix, base units (bytes, seconds, celsius, ratios 0–1), `_total`
on counters, and a singular noun for the thing being measured.

Prefer labels over metric names: `truenas_pool_status{pool="tank"}` rather than
`truenas_tank_status`.

## GitHub Actions

Every `uses:` is pinned to a **40-character commit SHA** with a `# vX.Y.Z`
comment. A floating tag is mutable — the same workflow can run different code
tomorrow, which is the supply-chain hole SHA pinning exists to close. The
comment is what makes the pin readable, and what Dependabot rewrites when it
bumps the SHA.

`make actions-pinned` enforces both halves in CI, so an unpinned action or a
pin without a version comment fails the PR.

When adding an action, resolve the current release rather than writing a version
from memory:

```bash
repo=actions/checkout
tag=$(gh api "repos/$repo/releases/latest" --jq .tag_name)
sha=$(gh api "repos/$repo/git/ref/tags/$tag" --jq .object.sha)
echo "$repo@$sha # $tag"
```

Annotated tags need one more hop — if `.object.type` is `tag`, dereference with
`gh api repos/$repo/git/tags/$sha --jq .object.sha` to reach the commit.

The same applies to tool versions hardcoded in `run:` steps (helm, kubeconform,
hadolint, actionlint). They go stale just as fast and get audited less. Check
the release asset name too — hadolint renamed its Linux binary from
`hadolint-Linux-x86_64` to `hadolint-linux-x86_64`, which a version bump alone
would have turned into a 404.

## Versioning

Semantic versioning. The release tag, `__version__` in
`src/truenas_scale_exporter/__init__.py`, and `appVersion` in `Chart.yaml` must
all agree — the release workflow fails the build if they do not.

Adding a metric is a minor bump. Renaming or removing one is a **major** bump,
because it silently breaks dashboards and alerting rules downstream.
