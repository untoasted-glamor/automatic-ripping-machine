# Recorded `makemkvcon` output

The `makemkvcon_*` files are real robot-mode output (stdout + stderr), replayed
by `tests/_makemkvcon_fake.py`. Long `PRGV` streams are thinned
(`*_prgv_thinned.txt`); every other line is verbatim.

`makemkvcon_dvd_short.txt` and `makemkvcon_no_titles.txt` predate the
recordings below and are hand-made.

## MakeMKV version

Every `makemkvcon_iso_sintel_*`, `makemkvcon_bd_hanako_*` and
`makemkvcon_dvd_acca_*` file was recorded on **MakeMKV 2.0.0** (2026-10-17).
The ripper image runs **1.18.4** (`services/ripper/Dockerfile`
`MAKEMKV_VERSION`), because 2.0.0 segfaults on unencrypted Blu-rays.

Two recordings are 2.0.0 behaviour that 1.18.4 is not expected to share; the
fake replays them only with `makemkv_version="2.0.0"`:

- `makemkvcon_bd_hanako_backup_dev_source_rejected_rc10.txt`: `backup dev:…`
  refused (exit 10, wants `disc:<N>`).
- `makemkvcon_bd_hanako_info_file_decrypted_backup_segfault_rc139.txt`: every
  `file:` read of a `--decrypt` Blu-ray backup segfaults (exit 139).

The robot-mode lines themselves (`TINFO`, `PRGV`, `MSG` codes) are assumed to
match across the two versions until 1.18.4 recordings say otherwise.

## Sources

| Prefix | Disc | Notes |
|---|---|---|
| `makemkvcon_iso_sintel_` | Sintel DVD ISO (`devtools/iso-smoke.sh`'s pinned image) | no drive; `_drive_seated` ones with a BD seated in `/dev/sr0` |
| `makemkvcon_bd_hanako_` | Blu-ray "ToiletBound HanakoKun D1" | disc, its encrypted backup (`backup disc:0`) and remuxes from it |
| `makemkvcon_dvd_acca_` | DVD ACCA_CS_D1 | disc, its backup (one ISO image) and remuxes from it |
