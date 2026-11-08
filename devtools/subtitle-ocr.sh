#!/usr/bin/env bash
# Make text subtitles (.srt) from PGS sidecars (.sup) by hand, or score the OCR
# against a hand-read reference, with the model built into the arm-transcode image.
#
#   bash devtools/subtitle-ocr.sh [--overwrite] [--threads N] <sup or folder>...
#       Writes <name>.srt next to each <name>.sup (a folder means every .sup in it
#       and below). An existing .srt is kept unless --overwrite. One JSON line of
#       stats per file.
#
#   bash devtools/subtitle-ocr.sh --check <reference.tsv> <sup>...
#       Prints exact lines, CER, missing lines and the italic tally against the
#       reference (format: arm_transcode/subtitle_ocr/check.py).
#
# Runs in ${ARM_TRANSCODE_IMAGE:-arm-transcode:latest} as the calling user, with
# no network. Build the image first: docker compose build arm-transcode
set -euo pipefail

image="${ARM_TRANSCODE_IMAGE:-arm-transcode:latest}"
module="arm_transcode.subtitle_ocr"
args=()
mounts=()
mounted=""
n=0

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

# Mount the folder holding $1 (or $1 itself if it is a folder), mode $2, and set `mounted` to its
# path in the container. Not called in $(...): the mount list must survive.
mount_path() {
    local path dir base
    path="$(realpath -e -- "$1")"
    if [[ -d "$path" ]]; then
        dir="$path"
        base=""
    else
        dir="$(dirname -- "$path")"
        base="/$(basename -- "$path")"
    fi
    n=$((n + 1))
    mounts+=(-v "$dir:/work/$n:$2")
    mounted="/work/$n$base"
}

[[ $# -gt 0 ]] || usage
mode="rw"
if [[ "$1" == "--check" ]]; then
    [[ $# -ge 3 ]] || usage
    module="arm_transcode.subtitle_ocr.check"
    mode="ro"
    shift
    mount_path "$1" ro
    args+=("$mounted")
    shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --overwrite) args+=("$1") ;;
        --threads)
            [[ $# -ge 2 ]] || usage
            args+=("$1" "$2")
            shift
            ;;
        -h | --help) usage ;;
        *)
            mount_path "$1" "$mode"
            args+=("$mounted")
            ;;
    esac
    shift
done

exec docker run --rm --network none \
    --user "$(id -u):$(id -g)" \
    --entrypoint /opt/venv/bin/python \
    "${mounts[@]}" \
    "$image" -m "$module" "${args[@]}"
