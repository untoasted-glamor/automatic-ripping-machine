#!/usr/bin/env bash
# Vendor the disc menu walk (fork only) into services/transcode/menu_core/.
#
# The menu preview image runs the Jellyfin disc menu plugin's player core
# under Node (`walk.mjs`, built by the plugin's `web/build.mjs`) to find every
# menu page and what each button plays. ARM builds no JavaScript itself, so
# the bundle is committed here with its SHA-256 and the plugin commit it came
# from; the Dockerfile's menu-preview target checks the hash, and
# services/transcode/tests/test_menu_core.py checks the three files agree.
#
# Usage: bash devtools/sync-menu-core.sh [<plugin checkout>]
#   default checkout: $ARM_MENU_CORE_SRC, else ~/jellyfin-plugin-disc-menu
# The checkout must be clean (the commit recorded must be what was built).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
ROOT_DIR="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
SRC="${1:-${ARM_MENU_CORE_SRC:-${HOME}/jellyfin-plugin-disc-menu}}"
DEST="${ROOT_DIR}/services/transcode/menu_core"

[[ -f "${SRC}/web/build.mjs" ]] || { echo "error: ${SRC} is not a disc menu plugin checkout" >&2; exit 1; }
if [[ -n "$(git -C "${SRC}" status --porcelain)" ]]; then
    echo "error: ${SRC} has uncommitted changes; commit them so the bundle names its source" >&2
    exit 1
fi
commit="$(git -C "${SRC}" rev-parse HEAD)"

echo "-> building the walk bundle in ${SRC} (${commit:0:12})"
( cd "${SRC}/web" && { [[ -d node_modules ]] || npm ci --no-audit --no-fund; } && node build.mjs >/dev/null )

mkdir -p "${DEST}"
cp "${SRC}/web/walk-dist/walk.mjs" "${DEST}/walk.mjs"
( cd "${DEST}" && sha256sum walk.mjs > walk.mjs.sha256 )
echo "${commit}" > "${DEST}/SOURCE_COMMIT"
echo "-> ${DEST}/walk.mjs $(cut -c1-12 "${DEST}/walk.mjs.sha256") from ${commit:0:12}"
