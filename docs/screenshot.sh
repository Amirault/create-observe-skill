#!/usr/bin/env bash
# Rebuild docs/dashboard.png from the fictional demo (needs Google Chrome or Chromium).
#
#   docs/screenshot.sh [chrome-binary]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chrome="${1:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

"${root}/examples/check-demo.sh" "${root}/.demo" > /dev/null
# Dates in English whatever the machine locale; the engine and its dashboard are unchanged.
sed 's/(undefined, {/("en-US", {/g' "${root}/.demo/state/dashboard.html" > "${root}/.demo/shot.html"
"${chrome}" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --window-size=1200,1300 \
  --virtual-time-budget=4000 --user-data-dir="${root}/.demo/chrome" \
  --screenshot="${root}/docs/dashboard.png" "file://${root}/.demo/shot.html" 2> /dev/null
echo "wrote ${root}/docs/dashboard.png"
