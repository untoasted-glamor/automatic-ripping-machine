"""Score the OCR against a hand-read reference: `python -m arm_transcode.subtitle_ocr.check <reference.tsv> <sup>...`.

The reference is a tab-separated file, one sampled text area per row:

    track  composition  area  italic  text

`track` is the sidecar's language part (`eng`, `eng.2`): a `.sup` whose name
ends in `.<track>.sup` is that track. `composition` and `area` are 0-based
indexes in the order `pgs.decode` and `areas.split` give them. `italic` is 1 or
0. In `text`, ` / ` is a line break; runs of spaces count as one (the reader
gives one). Lines starting with `#` are comments.

Prints exact lines, character error rate (edits / reference characters),
missing lines (more than half the characters wrong) and the italic tally,
then every line that is not exact.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from arm_transcode.subtitle_ocr import areas, engine, run


@dataclass(frozen=True)
class Sample:
    track: str
    composition: int
    area: int
    italic: bool
    text: str  # "\n" between lines


@dataclass(frozen=True)
class Score:
    lines: int
    exact: int
    missing: int
    edits: int
    characters: int
    italic_found: int
    italic_total: int
    upright_marked: int
    wrong: list[tuple[Sample, str]]
    italic_wrong: list[tuple[Sample, bool]]

    @property
    def cer(self) -> float:
        return self.edits / self.characters if self.characters else 0.0


def load_reference(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        track, composition, area, italic, text = line.split("\t", 4)
        text = "\n".join(re.sub(" +", " ", line).strip() for line in text.split(" / "))
        samples.append(Sample(track, int(composition), int(area), italic == "1", text))
    return samples


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def track_of(sidecar: Path, tracks: set[str]) -> str | None:
    """The longest reference track whose `.<track>.sup` ends the file name."""
    matches = [t for t in tracks if sidecar.name.endswith(f".{t}.sup")]
    return max(matches, key=len) if matches else None


def score(samples: list[Sample], found: dict[tuple[str, int, int], tuple[str, bool]]) -> Score:
    """`found` maps (track, composition, area) to the (text, italic) read there."""
    exact = missing = edits = characters = italic_found = upright_marked = 0
    wrong: list[tuple[Sample, str]] = []
    italic_wrong: list[tuple[Sample, bool]] = []
    for sample in samples:
        text, italic = found.get((sample.track, sample.composition, sample.area), ("", False))
        distance = edit_distance(sample.text, text)
        edits += distance
        characters += len(sample.text)
        if distance == 0:
            exact += 1
        else:
            wrong.append((sample, text))
        if distance > len(sample.text) / 2:
            missing += 1
        if sample.italic and italic:
            italic_found += 1
        if not sample.italic and italic:
            upright_marked += 1
        if sample.italic != italic:
            italic_wrong.append((sample, italic))
    return Score(
        lines=len(samples),
        exact=exact,
        missing=missing,
        edits=edits,
        characters=characters,
        italic_found=italic_found,
        italic_total=sum(1 for s in samples if s.italic),
        upright_marked=upright_marked,
        wrong=wrong,
        italic_wrong=italic_wrong,
    )


def read_samples(
    samples: list[Sample], sidecars: list[Path], reader: engine.LineReader
) -> dict[tuple[str, int, int], tuple[str, bool]]:
    tracks = {s.track for s in samples}
    found: dict[tuple[str, int, int], tuple[str, bool]] = {}
    for sidecar in sidecars:
        track = track_of(sidecar, tracks)
        if track is None:
            continue
        compositions, read_events = run.read_track(sidecar.read_bytes(), reader)
        by_digest = {r.event.area.digest: (r.reading.text, r.italic) for r in read_events}
        for sample in samples:
            if sample.track != track or sample.composition >= len(compositions):
                continue
            split = areas.split(compositions[sample.composition])
            if sample.area < len(split):
                found[(track, sample.composition, sample.area)] = by_digest[split[sample.area].digest]
    return found


def report(result: Score) -> str:
    lines = [
        f"lines {result.lines}, exact {result.exact}, missing {result.missing}, "
        f"CER {100 * result.cer:.2f}% ({result.edits} edits / {result.characters} chars)",
        f"italic found {result.italic_found}/{result.italic_total}, upright marked italic {result.upright_marked}",
    ]
    for sample, text in result.wrong:
        lines.append(f"  text   {sample.track} c{sample.composition} a{sample.area}: want {sample.text!r} got {text!r}")
    for sample, italic in result.italic_wrong:
        marked = "italic" if italic else "upright"
        lines.append(f"  italic {sample.track} c{sample.composition} a{sample.area}: read as {marked}: {sample.text!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arm_transcode.subtitle_ocr.check")
    parser.add_argument("reference", type=Path)
    parser.add_argument("sidecars", nargs="+", type=Path)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args(argv)

    samples = load_reference(args.reference)
    found = read_samples(samples, args.sidecars, engine.OnnxLineReader(threads=args.threads))
    print(report(score(samples, found)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
