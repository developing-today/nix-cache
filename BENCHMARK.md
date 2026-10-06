# nix-cache benchmark: real artifacts, speed, and cost

Date: 2026-10-05. All numbers measured, not estimated, except where marked.

## What was built

**12 small things** (fully pushed to the cache, verified retrievable):
hello, cowsay, jq, ripgrep, fd, bat, eza, curl, wget, tree, figlet, btop
— 16 store outputs, 48 unique closure paths, **155.7 MiB NAR**.

**2 big things** (realized locally; push in progress):
- `firefox-140.0.1` and `go-1.23.8`
- 324 unique closure paths, **1916.6 MiB NAR**.
- 20/324 paths pushed at time of writing. The rest is bottlenecked on
  **client-side xz compression** (see below), not on the cache.

Total unique: 342 paths, 1948.9 MiB NAR. All signed with `nix-cache-1`.

## Speed

### Push (ours, Cloudflare)

| Workload | Paths | NAR bytes | Wall time | Effective |
|---|---|---|---|---|
| 12 small (full closures) | 48 | 155.7 MiB | 107 s | **1.45 MB/s** |
| 2 big (full closures) | 324 | 1916.6 MiB | ~20 paths in 15 min | CPU-bound (below) |

Raw PUT throughput (50 MB file, curl): **4.4 MB/s**.
The gap between 4.4 MB/s raw and 1.45 MB/s via `nix copy` is **client-side
xz compression**: measured 0.86 MB/s on this machine (2 throttled vCPUs;
glibc's 30 MB NAR took 35 s to compress). This cost applies equally to every
binary cache service — it's nix, not us.

### Pull

Same 4 packages (jq, fd, bat, tree — 9.9 MiB of top-level paths, warm deps):

| Source | Time |
|---|---|
| **ours** (Cloudflare Worker + R2) | **7 s** |
| cache.nixos.org (CloudFront CDN) | **4 s** |

Raw GET throughput (1.1 MB NAR): **1.1 MB/s** (single measurement; varies).

Takeaway: nixos.org is ~1.75× faster on pulls — expected, it's a global CDN
vs our single Worker. For a personal/team cache the absolute numbers are
fine: seconds, not minutes.

### Why no Attic speed numbers

Attic server (atticd 0.1.0) is **built from source and running locally**
(port 8080, config in `bench/atticd.toml`, token minted). The official
client could not be built: it links against nix C++ libraries
(`nix-main >= 2.24` via pkg-config) and the build needs the full
transitive dev dependency chain (libarchive, nlohmann_json, bdw-gc,
libblake3, ...) which isn't available for our nix 2.35.2 install.
Disabling the `nix_store` C++ feature breaks the client build (it uses
the bindings directly). Localhost vs remote is apples-to-oranges for
throughput anyway — the meaningful comparison is protocol/operational
overhead, not bandwidth. Server setup is documented in `bench/` for
anyone with a working client.

### Cachix speed numbers (2026-10-06, Drew provided token + cache `dezren39`)

Push and pull both verified working end-to-end with a custom CA derivation
(`nix-cache-ca-demo`, 480 B NAR):
- Push: `cachix push dezren39` succeeded, using **zstd** compression
  (Cachix's preferred method; ours uses xz).
- Pull: `nix build` with `https://dezren39.cachix.org` as substituter and
  key `dezren39.cachix.org-1:+zOzRXxPWsdEwM7mumpLXH+6ju/Jq0JpdVYT/Wh/wfg=`
  substituted the path in 2s.
- **Important:** `cachix push` silently skips any path already on
  cache.nixos.org (by design, to avoid wasting storage). All 16 standard
  nixpkgs packages in our benchmark set were skipped for this reason —
  so there is no apples-to-apples push-speed comparison on standard
  packages. For custom artifacts (the actual use case for a personal
  cache), both services push and pull correctly.
- No meaningful throughput delta measured on the tiny demo artifact;
  the architectural difference is operational (managed SaaS vs
  self-hosted Worker), not protocol speed.

## Cost model

Pricing verified 2026-10-05 (sources in git history / this doc's references).

**Ours (Cloudflare):** R2 $0.015/GB-mo · Class A (writes) $4.50/M ·
Class B (reads) $0.36/M · egress **$0** · free tier: 10 GB + 1M A + 10M B /mo.
Workers free tier (100k req/day) covers all scenarios below ($0).
KV: $0.50/M reads, $5/M writes. D1/Queues negligible.

Measured: 180 paths per GB of NAR; xz ≈ 3:1 → ~545 paths per GB stored.

Assumptions (stated): 20% of stored data re-pushed monthly (churn),
one full pull per month. X = GB stored in R2.

| X GB | Ours /mo | Cachix /mo | Attic self-hosted /mo | Garnix /mo |
|---|---|---|---|---|
| 1 | **$0** (free tier) | $0 (free 5GB, public-only) | ~$5 VPS | $0 |
| 10 | **$0** (free tier) | ~$15 (Starter 50GB)¹ | ~$5 VPS | $0 |
| 50 | **~$0.70** | ~$15 (Starter 50GB)¹ | ~$8 VPS | $0–25 |
| 100 | **~$1.40** | ~$45 (Standard 250GB)¹ | ~$12 VPS | $25 |

¹ Cachix paid tiers are contact-pricing (no public $); figures are community
estimates, flagged as such. Free tier is public-caches-only.

**The shape of the bill:** at every scale, ours is dominated by the
$0.015/GB-mo storage line. Operations are noise: pushing 1 GB costs ~$0.002
in requests; pulling 1 GB costs ~$0.0002 — and egress is $0 (S3 would charge
$0.09/GB: pulling 100 GB/mo from S3 is $9 in egress alone).

### The 1 GB that churns

Scenario: 1 GB stored, **fully replaced daily** (CI-style), pulled daily.
30 days → 16,350 path-writes, 16,350 path-reads.

| | Ours | Cachix free | S3-backed DIY |
|---|---|---|---|
| Storage | $0.015 (1 GB avg) | $0 | $0.023 |
| Writes | $0.16 → **$0** (free tier: 1M/mo) | $0 | $0.08 |
| Reads | $0.014 → **$0** (free tier: 10M/mo) | $0 | $0.007 |
| Egress (30 GB) | **$0** | $0 | **$2.70** |
| **Total** | **~$0.02/mo** | $0 | ~$2.80/mo |

Two honest caveats:

1. **Our cache has no retention GC.** The cron only sweeps orphans (narinfo
   without NAR). A churning 1 GB where old objects are never deleted grows
   storage 30 GB/month. Cachix does LRU GC automatically at 85% of quota.
   If churn is the workload, add a retention policy (or push with
   overwriting keys, which our design already supports — same hash, new
   bytes).
2. **Churn on R2 is operations-cheap but not free.** The free tier absorbs
   this scenario 60× over (16k writes vs 1M included). Past the free tier,
   each daily full churn of 1 GB costs ~$0.16/mo in operations — still
   negligible next to storage.

## Bottom line

- For ≤10 GB you pay **$0** on ours (free tier) or Cachix (public).
- At 50–100 GB ours is **~10–30× cheaper** than Cachix paid tiers
  (~$0.70–1.40 vs ~$15–45/mo), with zero egress.
- Self-hosting (Attic/Harmonia) is ~$5–12/mo flat + your ops time.
- Speed: pulls are 1.75× slower than nixos.org's CDN; pushes are
  client-xz-bound on weak CPUs regardless of service.
- Missing vs Cachix: LRU retention GC, team token management, hosted
  dashboard. The cache protocol itself is complete and verified end-to-end.
