#!/bin/bash
# nix-cache-push-zstd.sh - Push a store path to nix-cache using zstd (fast) instead of xz.
# Usage: nix-cache-push-zstd.sh /nix/store/<hash>-<name>
# Same protocol as `nix copy --to`, but compresses with zstd for ~3x faster pushes.

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 /nix/store/<hash>-<name>" >&2
  exit 1
fi

STORE_PATH="$1"
HASH=$(basename "$STORE_PATH" | cut -d'-' -f1)
CACHE_URL="https://nix-cache.developing-today-llc-domains-0.workers.dev"
SECRET=$(cat ~/.config/nix-cache/upload-secret)
KEY_FILE=~/.config/nix-cache/signing.sec

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# Dump NAR
nix-store --dump "$STORE_PATH" > "$TMPDIR/nar"

# Compress with zstd (level 3 = good speed/ratio balance, same as cachix default)
zstd -3 -q "$TMPDIR/nar" -o "$TMPDIR/nar.zst"

# Hashes and sizes
FILE_HASH=$(nix-hash --type sha256 --flat "$TMPDIR/nar.zst")
FILE_SIZE=$(stat -c%s "$TMPDIR/nar.zst")
NAR_HASH=$(nix-store --query --hash "$STORE_PATH" | sed 's/sha256://')
NAR_SIZE=$(nix-store --query --size "$STORE_PATH")

# Metadata (deriver may be empty; don't let pipefail kill the script)
INFO_JSON=$(nix path-info --experimental-features 'nix-command' --json "$STORE_PATH" 2>/dev/null || echo '{}')
DERIVER_FULL=$(echo "$INFO_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(list(d.values())[0].get('deriver',''))" 2>/dev/null || echo "")
DERIVER=$(basename "$DERIVER_FULL" 2>/dev/null || echo "")
REFS=$(nix-store --query --references "$STORE_PATH" 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ' || echo "")

# Build narinfo
NARINFO="StorePath: $STORE_PATH
URL: nar/${FILE_HASH}.nar.zst
Compression: zstd
FileHash: sha256:${FILE_HASH}
FileSize: ${FILE_SIZE}
NarHash: sha256:${NAR_HASH}
NarSize: ${NAR_SIZE}
References: ${REFS}
Deriver: ${DERIVER}
"

# Sign if key available (ensure path is signed, then read sig from store)
if [ -f "$KEY_FILE" ]; then
  nix store sign --experimental-features 'nix-command' --key-file "$KEY_FILE" "$STORE_PATH" 2>/dev/null || true
  SIG=$(nix path-info --experimental-features 'nix-command' --json "$STORE_PATH" | python3 -c "import sys,json; d=json.load(sys.stdin); sigs=list(d.values())[0].get('signatures',[]); print(sigs[0] if sigs else '')")
  if [ -n "$SIG" ]; then
    NARINFO="${NARINFO}Sig: ${SIG}
"
  fi
fi

# Upload NAR
echo "Uploading NAR (${FILE_SIZE} bytes, zstd)..."
curl -s -X PUT --data-binary "@$TMPDIR/nar.zst" \
  "$CACHE_URL/upload/$SECRET/nar/${FILE_HASH}.nar.zst" | head -1

# Upload narinfo
echo "Uploading narinfo..."
echo -n "$NARINFO" | curl -s -X PUT --data-binary @- \
  "$CACHE_URL/upload/$SECRET/${HASH}.narinfo" | head -1

echo "Done: $STORE_PATH"
