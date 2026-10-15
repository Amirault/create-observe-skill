#!/usr/bin/env bash
# Generate the fictional tidy-code transcripts, build the observe-tidy-code dashboard from them
# and fail if the runs, outcomes or steps drift from what the demo is known to produce.
#
#   examples/check-demo.sh [work-dir]     (default: .demo/ at the repository root)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="${1:-${here}/../.demo}"
engine="${here}/../skills/create-observe-skill/scripts"
config="${here}/skills/observe-tidy-code/config.json"

rm -rf "${work}"
python3 "${here}/generate_transcripts.py" "${work}/projects"
summary="$(python3 "${engine}/observe.py" --config "${config}" --root "${work}/projects" --state "${work}/state" build)"
echo "${summary}"

if grep -q WARNING <<<"${summary}"; then
  echo "check-demo: some runs have causes that do not add up to their duration" >&2
  exit 1
fi

python3 - "${work}/state/sessions" <<'PY'
import glob, json, sys, collections
runs = [json.loads(line)["run"] for path in glob.glob(sys.argv[1] + "/*.jsonl") for line in open(path) if '"run"' in line[:8]]
outcomes = collections.Counter(run["outcome"] for run in runs)
steps = collections.Counter(step for run in runs for step in run.get("steps_seen", []))
print("outcomes:", dict(outcomes))
print("steps seen:", dict(steps))
expected = {"PR OPENED": 20, "ABORTED": 9, "ASKED HUMAN": 3}
assert len(runs) == 32, "expected 32 runs, got %d" % len(runs)
assert dict(outcomes) == expected, "outcomes drifted: %s" % dict(outcomes)
assert all(steps[name] for name in ("Step 1 — Pick the files", "Step 2 — Clean", "Step 3 — Review and PR")), steps
PY
echo "check-demo: OK, dashboard at ${work}/state/dashboard.html"
