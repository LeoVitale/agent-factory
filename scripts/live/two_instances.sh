#!/usr/bin/env bash
# Live check that two instances keep their Slack replies apart: a reply in an issue thread of one instance lands in
# that instance's state and in no other.
#
#   scripts/live/two_instances.sh [<slug-a> <slug-b>]
#
# With no slugs it takes the two instances in $FACTORY_HOME/instances (there must be exactly two). It prints one
# marker per instance; reply with each marker, exactly, in the Slack thread of one issue of that instance. Pick an
# issue whose stage is merged or dropped: those stages keep a reply in the issue's `inbox` and never hand it to a
# station, so the reply stays in state.json. The marker stays in that inbox afterwards; nothing reads it.
#
# Exit codes: 0 both replies landed only where they belong, 1 a check failed, 2 the replies did not arrive in time.
# WAIT_SECONDS (default 600) bounds the wait. MARKER_A and MARKER_B replace the generated markers.
set -euo pipefail

home="${FACTORY_HOME:-$HOME/agent-sandbox}"
wait_seconds="${WAIT_SECONDS:-600}"

if [ "$#" -eq 2 ]; then
  a="$1"; b="$2"
elif [ "$#" -eq 0 ]; then
  slugs=()
  for dir in "$home"/instances/*/; do
    [ -f "$dir/instance.json" ] && slugs+=("$(basename "$dir")")
  done
  if [ "${#slugs[@]}" -ne 2 ]; then
    echo "Expected two instances in $home/instances, found ${#slugs[@]}. Pass the two slugs." >&2
    exit 1
  fi
  a="${slugs[0]}"; b="${slugs[1]}"
else
  echo "usage: $0 [<slug-a> <slug-b>]" >&2
  exit 1
fi

for slug in "$a" "$b"; do
  status="$(docker inspect -f '{{.State.Status}}' "factory-$slug" 2>/dev/null || echo absent)"
  if [ "$status" != running ]; then
    echo "factory-$slug is $status; start it with \`factory up $slug\`." >&2
    exit 1
  fi
done

# Two instances on one Slack app would share its Socket Mode connections; compare hashes, never the tokens.
token_hash() {
  sed -n 's/^SLACK_APP_TOKEN=//p' "$home/instances/$1/.env" | shasum -a 256 | cut -c1-16
}
if [ "$(token_hash "$a")" = "$(token_hash "$b")" ]; then
  echo "$a and $b use the same SLACK_APP_TOKEN. Each instance needs its own Slack app." >&2
  exit 1
fi

marker_a="${MARKER_A:-c38-$a-$(openssl rand -hex 3)}"
marker_b="${MARKER_B:-c38-$b-$(openssl rand -hex 3)}"

python3 - "$home" "$a" "$b" "$marker_a" "$marker_b" "$wait_seconds" <<'PY'
import json
import sys
import time
from pathlib import Path

home, a, b, marker_a, marker_b, wait = sys.argv[1:7]
IDLE = ("merged", "dropped")


def state(slug):
    try:
        return json.loads((Path(home) / "instances" / slug / "state" / "state.json").read_text())
    except (OSError, ValueError):
        return {"issues": {}}


def holders(slug, marker):
    """Issues of the instance whose inbox holds the marker."""
    return [ident for ident, rec in state(slug)["issues"].items()
            if any(marker in (m.get("text") or "") for m in rec.get("inbox", []))]


for slug, marker in ((a, marker_a), (b, marker_b)):
    threads = [ident for ident, rec in state(slug)["issues"].items() if rec.get("stage") in IDLE and rec.get("slack_ts")]
    print(f"Reply exactly `{marker}` in the Slack thread of one of these {slug} issues: "
          + (", ".join(threads[:5]) or "(none yet: an issue needs a thread first)"))

deadline = time.time() + int(wait)
while time.time() < deadline and not (holders(a, marker_a) and holders(b, marker_b)):
    time.sleep(5)
if not (holders(a, marker_a) and holders(b, marker_b)):
    missing = [m for slug, m in ((a, marker_a), (b, marker_b)) if not holders(slug, m)]
    print("Timed out waiting for " + ", ".join(missing), file=sys.stderr)
    sys.exit(2)

problems = []
for slug, mine, other in ((a, marker_a, marker_b), (b, marker_b, marker_a)):
    print(f"{slug}: `{mine}` landed in {', '.join(holders(slug, mine))}")
    if holders(slug, other):
        problems.append(f"{slug} also received `{other}` in {', '.join(holders(slug, other))}")
print("\n".join(problems) or "Each reply landed only in its own instance.")
sys.exit(1 if problems else 0)
PY
