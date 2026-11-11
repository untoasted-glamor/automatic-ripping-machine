"""Disc menus on the ripper side (fork-only; design `docs/plans/2026-11-10-disc-menus-v5.md`).

- `tree`: one read-only view of a disc's files, from a folder, an image or the device (pycdlib, or 7-Zip for
  UDF 2.50), with no mount.
- `probe`: the scan-time `menu_kind` (HDMV, BD-J, DVD, none, unknown).
- `copy`: the menu's source files out of a backup or a file source into `<job raw>/menu/`.

The structures themselves are read by `arm_common.disc_menus`, shared with the transcoder's pack builders.
"""
