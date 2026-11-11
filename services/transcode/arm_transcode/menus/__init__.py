"""Disc menu packs (fork-only; design `docs/plans/2026-11-10-disc-menus-v5.md`, "Pack schemas").

A pack is the folder the Jellyfin Disc Menu plugin plays: `menu.json` plus the images and backgrounds it names,
flat. It is built from the menu the ripper copied (`<job raw>/menu/`, see `arm_ripper.menus.copy`) and the job's
encodes:

- `hdmv_pack`: Blu-ray (HDMV) menus, schema `hdmv-1`.
- `dvd_pack`: DVD menus, schema `dvd-1`.
- `model`: what a build is told (the disc, which encode each title is) and what it reports.
- `media`: the ffmpeg and ffprobe calls (lengths, chapters, backgrounds), behind a seam for tests.
- `images`: button and overlay images (Pillow).

`python -m arm_transcode.menus` builds one pack from a map file, by hand or from `devtools/`.
"""
