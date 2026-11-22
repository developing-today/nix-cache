#!/usr/bin/env python3
"""Read-only Cloudflare parity checks for nix-cache version promotion."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
API = "https://api.cloudflare.com/client/v4"
WORKER = "nix-cache"
EXPECTED_HOST = "nix-cache.developing-today-llc-domains-0.workers.dev"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def fail(message: str) -> None:
    raise SystemExit(message)


def get(path: str) -> dict[str, Any]:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", account, re.I) or not token:
        fail("Cloudflare environment credentials are missing or malformed")
    request = urllib.request.Request(
        f"{API}/accounts/{account}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        fail(f"Cloudflare read failed for {path}: {error}")
    if not payload.get("success"):
        fail(f"Cloudflare read failed for {path}: API returned success=false")
    return payload


def rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result", [])
    if isinstance(result, dict):
        result = result.get("items", result.get("deployments", result.get("versions", [])))
    return result if isinstance(result, list) else []


def latest_deployment() -> dict[str, Any]:
    deployments = rows(get(f"workers/scripts/{WORKER}/deployments"))
    if not deployments:
        fail("No current Worker deployment is visible; refusing promotion")
    try:
        deployment = max(
            deployments,
            key=lambda item: dt.datetime.fromisoformat(item["created_on"].replace("Z", "+00:00")),
        )
    except (KeyError, ValueError, TypeError):
        fail("Deployment API omitted a valid created_on value; refusing promotion")
    if not UUID.fullmatch(str(deployment.get("id", ""))):
        fail("Deployment API omitted a valid deployment ID; refusing promotion")
    versions = deployment.get("versions")
    if not isinstance(versions, list) or not versions:
        fail("Current deployment has no version traffic map; refusing promotion")
    total = 0
    normalized = []
    for item in versions:
        version_id = str(item.get("version_id", ""))
        pct = item.get("percentage")
        if not UUID.fullmatch(version_id) or not isinstance(pct, (int, float)) or pct <= 0:
            fail("Current deployment has an invalid version traffic map; refusing promotion")
        total += pct
        normalized.append({"version_id": version_id.lower(), "percentage": pct})
    if total != 100:
        fail("Current deployment traffic percentages do not total 100; refusing promotion")
    deployment["versions"] = sorted(normalized, key=lambda v: v["version_id"])
    return deployment


def config() -> dict[str, Any]:
    with (ROOT / "wrangler.toml").open("rb") as stream:
        return tomllib.load(stream)


def binding_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        values = []
        for name, binding in value.items():
            if isinstance(binding, dict):
                entry = dict(binding)
                entry.setdefault("name", name)
                values.append(entry)
        return values
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    fail("Worker version has an unknown bindings structure; refusing promotion")


def canonical_binding(binding: dict[str, Any]) -> tuple[str, str, str, str]:
    kind = str(binding.get("type", ""))
    name = str(binding.get("name", binding.get("binding", "")))
    if kind == "r2_bucket":
        target = str(binding.get("bucket_name", ""))
    elif kind == "kv_namespace":
        target = str(binding.get("namespace_id", binding.get("id", "")))
    elif kind in {"d1", "d1_database"}:
        kind = "d1"
        target = str(binding.get("database_id", binding.get("id", "")))
    elif kind == "queue":
        target = str(binding.get("queue_name", ""))
    elif kind in {"secret_text", "secret_key"}:
        # Never inspect or compare the secret value. Only ensure the binding
        # name/type survives upload so promotion cannot drop runtime secrets.
        target = "<redacted>"
    else:
        target = ""
    return kind, name, target, str(binding.get("class_name", ""))


def expected_bindings(cfg: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    result: set[tuple[str, str, str, str]] = set()
    for entry in cfg.get("r2_buckets", []):
        result.add(("r2_bucket", entry["binding"], entry["bucket_name"], ""))
    for entry in cfg.get("kv_namespaces", []):
        result.add(("kv_namespace", entry["binding"], entry["id"], ""))
    for entry in cfg.get("d1_databases", []):
        result.add(("d1", entry["binding"], entry["database_id"], ""))
    for entry in cfg.get("queues", {}).get("producers", []):
        result.add(("queue", entry["binding"], entry["queue"], ""))
    return result


def version(version_id: str) -> dict[str, Any]:
    if not UUID.fullmatch(version_id):
        fail("Version UUID is malformed")
    deployable = rows(get(f"workers/scripts/{WORKER}/versions?deployable=true"))
    if version_id.lower() not in {str(item.get("id", "")).lower() for item in deployable}:
        fail("Version is not in Cloudflare's deployable version set")
    payload = get(f"workers/scripts/{WORKER}/versions/{version_id}")
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("id", "").lower() != version_id.lower():
        fail("Cloudflare did not return the requested Worker version")
    resources = result.get("resources", {})
    runtime = resources.get("script_runtime", {})
    cfg = config()
    expected_date = cfg.get("compatibility_date")
    actual_date = str(runtime.get("compatibility_date", "")).split("T", 1)[0]
    if expected_date != actual_date:
        fail(f"Version {version_id} compatibility date differs from wrangler.toml")
    if sorted(runtime.get("compatibility_flags", [])) != sorted(cfg.get("compatibility_flags", [])):
        fail(f"Version {version_id} compatibility flags differ from wrangler.toml")
    seen = {canonical_binding(item) for item in binding_rows(resources.get("bindings", []))}
    non_secret = {item for item in seen if item[0] not in {"secret_text", "secret_key"}}
    if non_secret != expected_bindings(cfg):
        fail(f"Version {version_id} bindings differ from wrangler.toml")
    if not any(item[0] == "secret_text" and item[1] == "UPLOAD_SECRET" for item in seen):
        fail(f"Version {version_id} does not expose the existing UPLOAD_SECRET binding")
    return result


def runtime_signature(result: dict[str, Any]) -> dict[str, Any]:
    runtime = result.get("resources", {}).get("script_runtime", {})
    signature = dict(runtime)
    if "compatibility_date" in signature:
        signature["compatibility_date"] = str(signature["compatibility_date"]).split("T", 1)[0]
    signature["compatibility_flags"] = sorted(signature.get("compatibility_flags", []))
    return signature


def secret_names(result: dict[str, Any]) -> list[str]:
    bindings = binding_rows(result.get("resources", {}).get("bindings", []))
    return sorted(
        name for kind, name, _target, _class in map(canonical_binding, bindings)
        if kind in {"secret_text", "secret_key"}
    )


def check_parity() -> dict[str, Any]:
    cfg = config()
    deployment = latest_deployment()
    active_versions = [version(item["version_id"]) for item in deployment["versions"]]
    if not active_versions:
        fail("Current deployment has no readable active version")
    active_runtime = [runtime_signature(item) for item in active_versions]
    active_secrets = [secret_names(item) for item in active_versions]
    if any(signature != active_runtime[0] for signature in active_runtime[1:]):
        fail("Current serving versions have different runtime settings; refusing promotion")
    if any(names != active_secrets[0] for names in active_secrets[1:]):
        fail("Current serving versions have different secret binding names; refusing promotion")

    subdomain = get("workers/subdomain").get("result", {})
    worker_subdomain = get(f"workers/scripts/{WORKER}/subdomain").get("result", {})
    host = f"{WORKER}.{subdomain.get('subdomain', '')}.workers.dev"
    if host != EXPECTED_HOST or worker_subdomain.get("enabled") is not True:
        fail("Current workers.dev public route differs from README; refusing promotion")

    schedule_result = get(f"workers/scripts/{WORKER}/schedules").get("result", {})
    schedules = schedule_result.get("schedules", [])
    actual_crons = sorted(item.get("cron") for item in schedules)
    expected_crons = sorted(cfg.get("triggers", {}).get("crons", []))
    if actual_crons != expected_crons:
        fail("Current Cron triggers differ from wrangler.toml; refusing promotion")

    queues = rows(get("queues"))
    matching_queues = [q for q in queues if q.get("queue_name") == "nix-cache-indexing"]
    if len(matching_queues) != 1 or not matching_queues[0].get("queue_id"):
        fail("Could not resolve exactly one nix-cache-indexing Queue; refusing promotion")
    queue_id = matching_queues[0]["queue_id"]
    consumers = rows(get(f"queues/{queue_id}/consumers"))
    matching_consumers = [
        c for c in consumers
        if c.get("type") == "worker" and c.get("script_name") == WORKER
    ]
    if len(matching_consumers) != 1:
        fail("Current nix-cache Queue consumer is missing or ambiguous; refusing promotion")
    settings = matching_consumers[0].get("settings", {})
    consumer_cfg = cfg.get("queues", {}).get("consumers", [])
    if len(consumer_cfg) != 1 or (
        matching_consumers[0].get("queue_name") != consumer_cfg[0].get("queue")
        or settings.get("batch_size") != consumer_cfg[0].get("max_batch_size")
        or settings.get("max_retries") != consumer_cfg[0].get("max_retries")
    ):
        fail("Current Queue consumer settings differ from wrangler.toml; refusing promotion")

    return {
        "deployment_id": deployment["id"],
        "versions": deployment["versions"],
        "runtime": active_runtime[0],
        "secret_bindings": active_secrets[0],
        "route": host,
        "crons": actual_crons,
        "queue": "nix-cache-indexing",
        "queue_batch_size": settings["batch_size"],
        "queue_max_retries": settings["max_retries"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("snapshot", "verify-version", "verify-baseline", "verify-active"))
    parser.add_argument("--version-id")
    parser.add_argument("--baseline")
    parser.add_argument("--output")
    args = parser.parse_args()

    if args.command in {"snapshot", "verify-baseline"}:
        state = check_parity()
        if args.command == "verify-baseline":
            if not args.baseline:
                fail("--baseline is required")
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            if (state["deployment_id"] != baseline.get("deployment_id")
                or state["versions"] != baseline.get("versions")
                or state["runtime"] != baseline.get("runtime")
                or state["secret_bindings"] != baseline.get("secret_bindings")):
                fail("Current serving deployment changed since upload; refusing promotion")
            if not args.version_id:
                fail("--version-id is required")
            candidate = version(args.version_id)
            if runtime_signature(candidate) != baseline["runtime"]:
                fail("Candidate runtime settings differ from current production; refusing promotion")
            if secret_names(candidate) != baseline["secret_bindings"]:
                fail("Candidate secret binding names differ from current production; refusing promotion")
        if args.command == "snapshot":
            if not args.output:
                fail("--output is required")
            Path(args.output).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.command == "verify-version":
        if not args.version_id:
            fail("--version-id is required")
        version(args.version_id)
    elif args.command == "verify-active":
        if not args.version_id:
            fail("--version-id is required")
        state = latest_deployment()
        if state["versions"] != [{"version_id": args.version_id.lower(), "percentage": 100}]:
            fail("Serving deployment read-back did not confirm exactly the promoted version at 100 percent")
        check_parity()


if __name__ == "__main__":
    main()
