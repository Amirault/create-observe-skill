#!/usr/bin/env bash
# Rebuild the README images from the fictional demo, one dashboard card per image
# (needs Google Chrome or Chromium).
#
#   docs/screenshot.sh [chrome-binary]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chrome="${1:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
page="${root}/.demo/shot.html"

"${root}/examples/check-demo.sh" "${root}/.demo" > /dev/null
# Dates in English whatever the machine locale; then keep only the card named in the URL hash.
sed 's/(undefined, {/("en-US", {/g' "${root}/.demo/state/dashboard.html" > "${page}"
cat >> "${page}" <<'EOF'
<script>
window.addEventListener("load", () => setTimeout(() => {
  const want = decodeURIComponent(location.hash.slice(1));
  const main = document.querySelector("main");
  for (const node of [...main.children]) {
    const h2 = node.querySelector && node.querySelector("h2");
    if (!(node.tagName === "SECTION" && h2 && h2.textContent.startsWith(want))) node.style.display = "none";
  }
  const facts = document.querySelector("#detail-card .grid2");
  if (facts) facts.style.display = "none";
  const insights = document.getElementById("insights");
  insights.style.display = "none";
  insights.previousElementSibling.style.display = "none";
  let hide = false;
  for (const row of document.querySelectorAll("#changes tr")) {
    hide = hide || row.textContent.startsWith("Median counts");
    if (hide) row.style.display = "none";
  }
  const card = [...main.children].find(node => node.style.display !== "none");
  document.body.setAttribute("data-h", Math.ceil(card.getBoundingClientRect().bottom + 16));
}, 300));
</script>
EOF

shot() {
  local url="file://${page}#$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")"
  local height
  height="$("${chrome}" --headless=new --disable-gpu --window-size=1100,800 --virtual-time-budget=3000 \
    --user-data-dir="${root}/.demo/chrome" --dump-dom "${url}" 2> /dev/null | sed -n 's/.*data-h="\([0-9]*\)".*/\1/p')"
  "${chrome}" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size="1100,${height}" --virtual-time-budget=3000 --user-data-dir="${root}/.demo/chrome" \
    --screenshot="${root}/docs/$2.png" "${url}" 2> /dev/null
  echo "wrote docs/$2.png"
}

shot "Current performance" verdict
shot "What changed" what-changed
shot "Run spans" run-spans
shot "Run duration over time" trend
