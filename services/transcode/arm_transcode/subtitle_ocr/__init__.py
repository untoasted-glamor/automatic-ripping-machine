"""Text subtitles (`.srt`) from image subtitle sidecars (PGS `.sup`), by OCR.

Pipeline: `pgs` decodes the compositions, `areas` splits them into text areas
and follows them through time, `engine` reads each unique area, `italic`
measures its slant, `srt` writes the cues, and `run` ties them together for one
sidecar.
"""
