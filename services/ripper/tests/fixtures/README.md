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

Two recordings are 2.0.0 behaviour that 1.18.4 is not expected to share
(not yet re-checked: it needs a Blu-ray, S17 17.4); the fake replays them only
with `makemkv_version="2.0.0"`:

- `makemkvcon_bd_hanako_backup_dev_source_rejected_rc10.txt`: `backup dev:…`
  refused (exit 10, wants `disc:<N>`).
- `makemkvcon_bd_hanako_info_file_decrypted_backup_segfault_rc139.txt`: every
  `file:` read of a `--decrypt` Blu-ray backup segfaults (exit 139).

`makemkvcon_dvd_acca_info_file_on_iso_backup_rc10_*.txt` is also 2.0.0 only:
1.18.4 reads the image through `file:` like `iso:` (below).

## 1.18.4 re-check (no disc, 2026-10-20)

The Sintel ISO and the ACCA image (`bk.iso`) were re-read on 1.18.4 in a
throwaway `arm-ripper` container with no drive (track-select plan, S17 17.2
and 17.3). Their title tables (`TCOUNT`/`CINFO`/`TINFO`/`SINFO`) are
byte-identical to the 2.0.0 recordings at minlength 0, 120 (default) and 600,
with and without `--noscan`, and the ACCA image's still equal the disc's.
Single-title `mkv` runs have the same `MSG`/`PRGT`/`PRGC` sequence (plus
`MSG:5042` with no drive, and `MSG:5074` on a first run); a missing id
still exits 12 silently. Output files are 4-22 B larger than 2.0.0's. So the
2.0.0 recordings stand in for 1.18.4, and only these are kept:

| File | What it shows |
|---|---|
| `makemkvcon_iso_sintel_info_minlength0_v1184.txt` | a 1.18.4 table, identical to the 2.0.0 one |
| `makemkvcon_iso_sintel_mkv_t0_v1184.txt` | a 1.18.4 single-title remux |
| `makemkvcon_iso_sintel_mkv_t0_outdir_missing_v1184.txt` | `mkv` into a directory that doesn't exist: `MSG:2019`, `MSG:5003`, `MSG:5037 "0 titles saved, 1 failed"`, **exit 0** and no file |
| `makemkvcon_dvd_acca_info_file_on_iso_backup_noscan_v1184.txt` | `file:` on the DVD image: exit 0 with the `iso:` table (2.0.0: exit 10) |
| `makemkvcon_dvd_acca_mkv_file_on_iso_backup_minlength0_t2_v1184.txt` | a remux through `file:` on the image: same file as through `iso:` |

## Sources

| Prefix | Disc | Notes |
|---|---|---|
| `makemkvcon_iso_sintel_` | Sintel DVD ISO (`devtools/iso-smoke.sh`'s pinned image) | no drive; `_drive_seated` ones with a BD seated in `/dev/sr0` |
| `makemkvcon_bd_hanako_` | Blu-ray "ToiletBound HanakoKun D1" | disc, its encrypted backup (`backup disc:0`) and remuxes from it |
| `makemkvcon_dvd_acca_` | DVD ACCA_CS_D1 | disc, its backup (one ISO image) and remuxes from it |
