#!/usr/bin/env bash
# Links the `factory` command from this clone into ~/.local/bin.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
ln -sf "$here/bin/factory" "$HOME/.local/bin/factory"
echo "factory -> $here/bin/factory"
