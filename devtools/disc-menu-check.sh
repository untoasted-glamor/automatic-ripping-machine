#!/usr/bin/env bash
# Disc menus, end to end on the Sintel DVD ISO (no drive, no stack):
#
#   bash devtools/disc-menu-check.sh [--keep] [--no-ffmpeg]
#
#   1. Probes the ISO's menu kind and copies its menus with this checkout's ripper code
#      (arm_ripper.menus), as the ripper does after a rip.
#   2. Builds the dvd-1 menu pack from that copy with this checkout's transcode code
#      (arm_transcode.menus), in ${ARM_TRANSCODE_IMAGE:-arm-transcode:latest} for ffmpeg, with no
#      network. The film itself is a stand-in file: the media cells come from the DVD's cell times.
#   3. Checks the copy and the pack against what Sintel is known to hold: five button menus in
#      title set 1, each with a motion background, and eight cells in title 2.
#
# --keep leaves the work folder (copy, map, pack) in place and prints its path.
# --no-ffmpeg builds without backgrounds and skips the image.
#
# The ISO is ${ISO_CACHE_DIR:-~/arm-corpus}/sintel.iso, the one devtools/iso-smoke.sh caches; it is
# fetched from archive.org when missing. Build the image first: docker compose build arm-transcode
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="${ISO_CACHE_DIR:-${HOME}/arm-corpus}"
ISO_PATH="${CACHE_DIR}/sintel.iso"
ISO_SHA256="7ea69a0215cdd7fff4acb48976677b2814a3d2e375b82b3288324bb356afe803"
ARCHIVE_URL="https://archive.org/download/sintel_20260427/sintel.iso"
IMAGE="${ARM_TRANSCODE_IMAGE:-arm-transcode:latest}"
KEEP=0
FFMPEG=1

log() { printf '\033[1;36m→\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; }

usage() {
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --no-ffmpeg) FFMPEG=0 ;;
        -h | --help) usage ;;
        *)
            err "unknown argument: $arg"
            usage
            ;;
    esac
done

iso_ok() {
    [[ -f "${ISO_PATH}" ]] && echo "${ISO_SHA256}  ${ISO_PATH}" | sha256sum --check --status
}

if ! iso_ok; then
    mkdir -p "${CACHE_DIR}"
    log "fetching ${ARCHIVE_URL}"
    curl -fL --progress-bar "${ARCHIVE_URL}" -o "${ISO_PATH}"
    iso_ok || {
        err "SHA-256 mismatch: ${ISO_PATH}"
        exit 1
    }
fi
ok "ISO: ${ISO_PATH}"

if [[ "${FFMPEG}" == 1 ]] && ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    err "no ${IMAGE} image; build it (docker compose build arm-transcode) or pass --no-ffmpeg"
    exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/arm-disc-menu-check.XXXXXX")"
cleanup() {
    if [[ "${KEEP}" == 1 ]]; then
        log "work folder kept: ${WORK}"
    else
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT

log "copying the menus"
uv run --project "${ROOT_DIR}" python - "${ISO_PATH}" "${WORK}/menu" <<'PY'
import json
import sys
from pathlib import Path

from arm_ripper.menus import copy, probe, tree

src, dest = sys.argv[1], Path(sys.argv[2])
kind = probe.probe_menu_kind(src)
t = tree.open_tree(src)
assert t is not None, "the ISO didn't open"
try:
    r = copy.copy_menu(t, dest)
finally:
    t.close()
print(json.dumps({"kind": kind, "disc": r.disc, "files": r.files, "bytes": r.bytes, "missing": r.missing,
                  "title_sets": r.title_sets, "complete": r.complete}))  # fmt: skip
assert kind == "dvd", f"menu kind {kind}, not dvd"
assert r.complete, f"copy incomplete, missing {r.missing}"
PY
ok "menu copy: ${WORK}/menu"

mkdir -p "${WORK}/show"
: >"${WORK}/show/Sintel (2010).mkv"
echo '{"name": "Sintel", "titles": {"2:1": [{"cells": [1, 8], "file": "Sintel (2010).mkv"}]}}' >"${WORK}/map.json"

log "building the menu pack"
if [[ "${FFMPEG}" == 1 ]]; then
    docker run --rm --network none -u "$(id -u):$(id -g)" \
        -v "${ROOT_DIR}/services/transcode/arm_transcode:/app/services/transcode/arm_transcode:ro" \
        -v "${ROOT_DIR}/packages/arm_common/arm_common:/app/packages/arm_common/arm_common:ro" \
        -v "${WORK}:/w" --entrypoint python "${IMAGE}" \
        -m arm_transcode.menus /w/menu /w/map.json /w/show /w/pack >"${WORK}/report.json"
else
    uv run --project "${ROOT_DIR}" python -m arm_transcode.menus \
        "${WORK}/menu" "${WORK}/map.json" "${WORK}/show" "${WORK}/pack" --no-ffmpeg >"${WORK}/report.json"
fi

uv run --project "${ROOT_DIR}" python - "${WORK}" "${FFMPEG}" <<'PY'
import json
import sys
from pathlib import Path

work, ffmpeg = Path(sys.argv[1]), sys.argv[2] == "1"
report = json.loads((work / "report.json").read_text())
pack = json.loads((work / "pack/menu.json").read_text())
problems = []


def check(cond, what):
    if not cond:
        problems.append(what)


check(pack["schema"] == "dvd-1", f"schema {pack['schema']}")
check(pack["menu_lang"] == "en", f"menu language {pack['menu_lang']}")
check(len(pack["vmg"]["titles"]) == 7, f"{len(pack['vmg']['titles'])} titles, not 7")
menus = [c["menu"] for p in pack["vts"]["1"]["menus"]["en"] for c in p["cells"] if c.get("menu")]
check([len(m["buttons"]) for m in menus] == [1, 3, 10, 7, 9], f"buttons per menu {[len(m['buttons']) for m in menus]}")
if ffmpeg:
    check(all(m["background"] and "video" in m["background"] for m in menus), "a menu without a motion background")
cells = pack["media"].get("2:1", {}).get("cells", {})
check(sorted(cells, key=int) == [str(i) for i in range(1, 9)], f"title 2 cells {sorted(cells)}")
offsets = [cells[str(i)]["offset"] for i in range(1, 9)] if len(cells) == 8 else []
check(offsets == sorted(offsets) and offsets[:1] == [0.0], f"cell offsets {offsets}")
files = {p.name for p in (work / "pack").iterdir()}
named = set()
for m in menus:
    named |= {v for v in (m["background"] or {}).values()} | ({m["overlay"]} if m.get("overlay") else set())
    named |= {b[s] for b in m["buttons"] for s in ("selected", "activated") if s in b}
check(named <= files, f"pack files missing: {sorted(named - files)}")
check(report["files"] == len(files), f"report says {report['files']} files, pack has {len(files)}")
print(json.dumps({k: report[k] for k in ("files", "bytes", "cells", "menus")}))
for n in report["notes"]:
    print("  note:", n.splitlines()[0])
if problems:
    print("\n".join("FAIL: " + p for p in problems))
    sys.exit(1)
PY
ok "menu pack checks passed"
