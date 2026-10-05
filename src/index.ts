// nix-cache: a Nix binary cache on Cloudflare.
// Worker + R2 (nar bytes) + KV (hot narinfo index) + D1 (system of record)
// + Queues (async indexing) + Cron (consistency sweep).
//
// Binary-cache protocol:
//   GET  /nix-cache-info             cache metadata
//   GET  /<hash>.narinfo             narinfo (KV, D1 fallback)
//   GET  /nar/<file>                NAR bytes (R2)
//   PUT  /upload/<secret>/<hash>.narinfo
//   PUT  /upload/<secret>/nar/<file>

export interface Env {
  NARS: R2Bucket;
  INDEX: KVNamespace;
  DB: D1Database;
  INDEX_QUEUE: Queue<IndexMessage>;
  UPLOAD_SECRET: string;
}

interface IndexMessage {
  hash: string;
  storePath: string;
  url: string;
  compression: string;
  fileHash: string;
  fileSize: number;
  narHash: string;
  narSize: number;
  refs: string;
  deriver: string;
  ca: string;
  sig: string;
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function parseNarinfo(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

function num(s: string | undefined): number {
  const n = parseInt(s ?? "", 10);
  return Number.isFinite(n) ? n : 0;
}

// Rebuild narinfo text from a D1 row (KV-miss fallback path).
function renderNarinfo(row: Record<string, unknown>): string {
  const str = (k: string) => String(row[k] ?? "");
  const lines = [
    `StorePath: ${str("store_path")}`,
    `URL: ${str("url")}`,
    `Compression: ${str("compression") || "none"}`,
    `FileHash: ${str("file_hash")}`,
    `FileSize: ${str("file_size")}`,
    `NarHash: ${str("nar_hash")}`,
    `NarSize: ${str("nar_size")}`,
    `References: ${str("refs")}`,
  ];
  if (str("deriver")) lines.push(`Deriver: ${str("deriver")}`);
  if (str("sig")) lines.push(`Sig: ${str("sig")}`);
  if (str("ca")) lines.push(`CA: ${str("ca")}`);
  return lines.join("\n") + "\n";
}

async function handlePut(req: Request, env: Env, key: string): Promise<Response> {
  if (key.endsWith(".narinfo")) {
    const text = await req.text();
    const f = parseNarinfo(text);
    if (!f["StorePath"] || !f["URL"]) {
      return new Response("narinfo missing StorePath/URL", { status: 400 });
    }
    const hash = key.slice(0, -".narinfo".length);
    await env.INDEX.put(`ni:${hash}`, text);
    const msg: IndexMessage = {
      hash,
      storePath: f["StorePath"],
      url: f["URL"],
      compression: f["Compression"] ?? "",
      fileHash: f["FileHash"] ?? "",
      fileSize: num(f["FileSize"]),
      narHash: f["NarHash"] ?? "",
      narSize: num(f["NarSize"]),
      refs: f["References"] ?? "",
      deriver: f["Deriver"] ?? "",
      ca: f["CA"] ?? "",
      sig: f["Sig"] ?? "",
    };
    await env.INDEX_QUEUE.send(msg);
    return new Response("indexed\n");
  }
  // NAR bytes -> R2, streamed, never buffered in the Worker.
  await env.NARS.put(key, req.body, {
    httpMetadata: { contentType: "application/octet-stream" },
  });
  return new Response("stored\n");
}

async function handleGet(req: Request, env: Env, key: string): Promise<Response> {
  if (key.endsWith(".narinfo")) {
    const hash = key.slice(0, -".narinfo".length);
    let body = await env.INDEX.get(`ni:${hash}`);
    if (body === null) {
      const row = await env.DB.prepare("SELECT * FROM artifacts WHERE hash = ?")
        .bind(hash)
        .first<Record<string, unknown>>();
      if (!row) return new Response("not in cache\n", { status: 404 });
      body = renderNarinfo(row);
      await env.INDEX.put(`ni:${hash}`, body); // re-warm KV
    }
    return new Response(body, { headers: { "Content-Type": "text/x-nix-narinfo" } });
  }
  const obj = await env.NARS.get(key);
  if (!obj) return new Response("not in cache\n", { status: 404 });
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  if (obj.httpEtag) headers.set("ETag", obj.httpEtag);
  return new Response(obj.body, { headers });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/nix-cache-info") {
      return new Response("StoreDir: /nix/store\nWantMassQuery: 1\nPriority: 30\n", {
        headers: { "Content-Type": "text/x-nix-cache-info" },
      });
    }

    if (path === "/" || path === "/index.html") {
      const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM artifacts")
        .first<{ n: number }>()
        .catch(() => null);
      return Response.json({ service: "nix-cache", artifacts: row?.n ?? -1 });
    }

    // Uploads go through /upload/<secret>/... so the bearer secret never
    // needs a custom header (nix copy --to can't send one).
    if (req.method === "PUT" && path.startsWith("/upload/")) {
      const rest = path.slice("/upload/".length);
      const slash = rest.indexOf("/");
      if (slash < 0) return new Response("bad upload path\n", { status: 400 });
      const secret = rest.slice(0, slash);
      const key = rest.slice(slash + 1);
      if (!env.UPLOAD_SECRET || !timingSafeEqual(secret, env.UPLOAD_SECRET)) {
        return new Response("forbidden\n", { status: 403 });
      }
      if (!key || key.includes("..")) return new Response("bad key\n", { status: 400 });
      return handlePut(req, env, key);
    }

    if (req.method === "GET" || req.method === "HEAD") {
      const key = path.replace(/^\//, "");
      if (!key || key.includes("..")) return new Response("not found\n", { status: 404 });
      if (/^[0-9a-z]{32}\.narinfo$/.test(key) || key.startsWith("nar/")) {
        return handleGet(req, env, key);
      }
    }
    return new Response("not found\n", { status: 404 });
  },

  // Async indexing: verify the NAR landed, then upsert the system of record.
  // Throws (=> queue retry) if the NAR isn't in R2 yet.
  async queue(batch: MessageBatch<IndexMessage>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      const m = msg.body;
      const head = await env.NARS.head(m.url);
      if (!head) throw new Error(`nar not yet in R2 for ${m.hash}; retrying`);
      await env.DB.prepare(
        `INSERT INTO artifacts
           (hash, store_path, url, compression, file_hash, file_size,
            nar_hash, nar_size, refs, deriver, ca, sig, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())
         ON CONFLICT(hash) DO UPDATE SET
           store_path=excluded.store_path, url=excluded.url,
           compression=excluded.compression, file_hash=excluded.file_hash,
           file_size=excluded.file_size, nar_hash=excluded.nar_hash,
           nar_size=excluded.nar_size, refs=excluded.refs,
           deriver=excluded.deriver, ca=excluded.ca, sig=excluded.sig`
      )
        .bind(
          m.hash, m.storePath, m.url, m.compression, m.fileHash, m.fileSize,
          m.narHash, m.narSize, m.refs, m.deriver, m.ca, m.sig
        )
        .run();
      msg.ack();
    }
  },

  // Daily consistency sweep: drop narinfo index entries whose NAR is gone.
  async scheduled(_event: ScheduledEvent, env: Env): Promise<void> {
    let cursor: string | undefined;
    let checked = 0;
    let orphans = 0;
    do {
      const page = await env.INDEX.list({ prefix: "ni:", limit: 100, cursor });
      for (const k of page.keys) {
        checked++;
        const text = await env.INDEX.get(k.name);
        if (!text) continue;
        const f = parseNarinfo(text);
        if (f["URL"] && !(await env.NARS.head(f["URL"]))) {
          orphans++;
          await env.INDEX.delete(k.name);
          await env.DB.prepare("DELETE FROM artifacts WHERE hash = ?")
            .bind(k.name.slice(3))
            .run();
        }
      }
      cursor = page.list_complete ? undefined : page.cursor;
    } while (cursor);
    console.log(`nix-cache sweep: checked ${checked} narinfos, removed ${orphans} orphans`);
  },
};
