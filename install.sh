#!/usr/bin/env bash
# Install the create-observe-skill skill into a skills folder.
#
#   ./install.sh <project-dir> [--target claude|agents|both]   # <project>/.claude/skills (default) or .agents/skills
#   ./install.sh --user                                         # ~/.claude/skills, for every project
#
# Install it in the skills folder that holds the skill you want to observe: the observe-<skill>
# skills it creates call the engine by its path relative to that folder.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skill="${here}/skills/create-observe-skill"

if [[ $# -lt 1 || "$1" == -h || "$1" == --help ]]; then
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

install_into() {
  local destination="$1/create-observe-skill"
  mkdir -p "$1"
  rm -rf "${destination}"
  cp -R "${skill}" "${destination}"
  find "${destination}" -name __pycache__ -type d -prune -exec rm -rf {} +
  echo "installed: ${destination}"
}

if [[ "$1" == --user ]]; then
  install_into "${HOME}/.claude/skills"
else
  project="$(cd "$1" && pwd)"
  shift
  target="claude"
  if [[ $# -ge 2 && "$1" == --target ]]; then target="$2"; fi
  case "${target}" in
    claude) install_into "${project}/.claude/skills" ;;
    agents) install_into "${project}/.agents/skills" ;;
    both) install_into "${project}/.claude/skills"; install_into "${project}/.agents/skills" ;;
    *) echo "install: --target must be claude, agents or both" >&2; exit 1 ;;
  esac
fi

cat <<'NEXT'

Next, in Claude Code:  /create-observe-skill <skill>   (or "create observe-<skill>")
It surveys the skill's past runs, writes observe-<skill>/config.json and SKILL.md, builds and checks the dashboard.
NEXT
