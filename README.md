# nix-cache

A Nix binary cache running on Cloudflare. Push with `nix copy --to`, pull
with the cache as a substituter. Built for content-addressed derivations,
where the output store path is derived from content — so any machine that
builds the same derivation gets the identical path, which is what makes a
shared cache correct.

## Architecture

Each Cloudflare primitive does the job it's best at:

| Primitive | Role |
|---|---|
| Workers | Front door: auth, routing, the binary-cache HTTP protocol. Stateless. |
| R2 | The bytes: NAR files. Cheap, zero egress fees. |
| Cache API (via R2) | Hot NARs served without re-reading storage. |
| KV | Hot index: `<hash>.narinfo` bodies, millisecond global reads. |
| D1 | System of record: every artifact's metadata, queryable with SQL. |
| Queues | Async indexing: PUT returns fast, a consumer verifies the NAR landed and upserts D1 (with retries). |
| Cron | Daily consistency sweep: drops narinfo entries whose NAR is gone. |

Deliberately no Durable Objects: the cache protocol's PUTs are atomic and
idempotent, and the queue already gives us verified, retried indexing. A DO
per store path would add a coordination hop with no payoff. (Where a DO
*would* fit: a live websocket feed of cache activity.)

## Protocol

```
GET  /nix-cache-info                  cache metadata (StoreDir, WantMassQuery, Priority)
GET  /<hash>.narinfo                   narinfo (KV, D1 fallback re-warms KV)
GET  /nar/<file>                      NAR bytes (R2, streamed)
PUT  /upload/<secret>/<hash>.narinfo  authenticated upload
PUT  /upload/<secret>/nar/<file>      authenticated upload
```

Uploads carry the secret as a URL path segment because `nix copy --to`
can't send custom headers — the path prefix is part of the store URL, so
nix PUTs to it naturally. Reads use the bare root URL (public).

## Setup

```bash
npm install
wrangler r2 bucket create nix-cache-nars
wrangler kv namespace create INDEX
wrangler d1 create nix-cache-db
wrangler queues create nix-cache-indexing
# put the returned KV id / D1 database_id into wrangler.toml
wrangler d1 execute nix-cache-db --remote --file schema.sql
openssl rand -hex 16 | wrangler secret put UPLOAD_SECRET
wrangler deploy
```

## Client config (nix.conf)

```
experimental-features = nix-command flakes ca-derivations
substituters = https://nix-cache.developing-today-llc-domains-0.workers.dev
trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWmi5qJeh0s9ZjeDkcQ5mmy43nxxA= nix-cache-1:x/kKZuzyQI3L0/twmOp3w2U68F2ZgM3WnH3ERYKrwf8=
```

Generate the cache keypair with `nix key generate-secret --key-name nix-cache-1`
(+ `nix key convert-secret-to-public`), sign paths before upload with
`nix store sign --key-file <secret> <path>`, then
`nix copy --to 'https://nix-cache.developing-today-llc-domains-0.workers.dev/upload/<secret>' <path>`.

## Demo

See `demo/flake.nix`: a content-addressed derivation with a 10s sleep, so a
cache hit (instant) is distinguishable from a rebuild. Verified end-to-end
2026-10-05:

1. `nix build .#demo` → built `/nix/store/0l22482...-nix-cache-ca-demo` (93s)
2. `nix store sign` + `nix copy --to 'https://…/upload/<secret>'` → narinfo +
   NAR in R2, metadata in KV + D1 (via queue)
3. `nix store delete` the output + drv locally
4. `nix build` with **only** this cache as substituter and only its key
   trusted → `copying path … from 'https://nix-cache.developing-today-llc-domains-0.workers.dev'`
   in **1.8s** — substituted, not rebuilt.
