#!/usr/bin/env bash
# Every offline test: the script tests, then the unittest suites. Needs only python3 and git.
set -euo pipefail
cd "$(dirname "$0")/.."
status=0
for t in tests/test_channel_broadcast.py tests/test_ci_after_judge.py tests/test_debt_feature.py \
         tests/test_demo_station.py tests/test_flow_speed.py tests/test_messages.py tests/test_review_requests.py; do
  if python3 "$t" > /tmp/factory-test.log 2>&1; then echo "ok   $t"; else echo "FAIL $t"; cat /tmp/factory-test.log; status=1; fi
done
python3 -m unittest tests.test_config tests.test_cli tests.test_fix_round tests.test_canvas_secrets tests.test_plan_approval tests.test_language tests.test_broadcast || status=1
exit $status
