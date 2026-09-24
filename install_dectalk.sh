#!/usr/bin/env bash
# Install the community DECtalk build (https://github.com/dectalk/dectalk)
# into ~/.local/share/dectalk, where voice-files looks for it.
set -euo pipefail

RELEASE="2023-10-30"
ASSET="ubuntu-latest.tar.gz"
DEST="${DECTALK_HOME:-$HOME/.local/share/dectalk}"
URL="https://github.com/dectalk/dectalk/releases/download/$RELEASE/$ASSET"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading DECtalk $RELEASE ..."
curl -fL --progress-bar -o "$tmp/$ASSET" "$URL"

mkdir -p "$DEST"
tar -xzf "$tmp/$ASSET" -C "$DEST"

if "$DEST/say" -fo stdout:raw -a "test" >/dev/null 2>&1; then
    echo "DECtalk installed in $DEST"
else
    echo "DECtalk was unpacked to $DEST but failed to run." >&2
    exit 1
fi
