"""Subtitle OCR (arm_transcode.subtitle_ocr): PGS decode, areas and events, the reader's
preprocessing and decode, italics, SRT, the runner and check, and the transcode task
(one reader process per sidecar), with a fake reader.

No model is loaded: `OnnxLineReader` is exercised with a fake ONNX session. The real
model is checked by `devtools/subtitle-ocr.sh --check` (see services/transcode/README.md).
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from arm_common.subtitle_ocr import BUNDLED_MODEL
from arm_transcode.subtitle_ocr import __main__ as cli
from arm_transcode.subtitle_ocr import areas, check, engine, italic, pgs, run, srt, task

# ---------------------------------------------------------------- a small PGS encoder

WHITE = (235, 128, 128, 255)  # Y, Cr, Cb, alpha
GREY = (100, 128, 128, 255)  # below the ink cut
SCREEN = (1920, 1080)


def segment(kind: int, ms: float, payload: bytes) -> bytes:
    return b"PG" + struct.pack(">II", int(ms * 90), 0) + bytes([kind]) + struct.pack(">H", len(payload)) + payload


def rle(index: np.ndarray) -> bytes:
    out = bytearray()
    for row in index.tolist():
        x = 0
        while x < len(row):
            colour, n = row[x], 1
            while x + n < len(row) and row[x + n] == colour and n < 16383:
                n += 1
            if colour != 0 and n < 3:
                out += bytes([colour]) * n
            elif colour == 0:
                out += bytes([0, n]) if n < 64 else bytes([0, 0x40 | (n >> 8), n & 0xFF])
            else:
                out += bytes([0, 0x80 | n, colour]) if n < 64 else bytes([0, 0xC0 | (n >> 8), n & 0xFF, colour])
            x += n
        out += b"\x00\x00"
    return bytes(out)


def pcs(
    ms: float,
    objects: Sequence[tuple[int, int, int] | tuple[int, int, int, tuple[int, int, int, int]]],
    *,
    epoch_start: bool = False,
    palette_id: int = 0,
    screen: tuple[int, int] = SCREEN,
) -> bytes:
    payload = struct.pack(
        ">HHBHBBBB", screen[0], screen[1], 0x10, 0, 0x80 if epoch_start else 0, 0, palette_id, len(objects)
    )
    for obj in objects:
        crop = obj[3] if len(obj) == 4 else None
        payload += struct.pack(">HBBHH", obj[0], 0, 0x80 if crop else 0, obj[1], obj[2])
        if crop:
            payload += struct.pack(">HHHH", *crop)
    return segment(pgs.PCS, ms, payload)


def pds(ms: float, entries: dict[int, tuple[int, int, int, int]], palette_id: int = 0) -> bytes:
    payload = bytes([palette_id, 0]) + b"".join(bytes([i, *e]) for i, e in entries.items())
    return segment(pgs.PDS, ms, payload)


def ods(ms: float, object_id: int, index: np.ndarray, parts: int = 1) -> bytes:
    data = rle(index)
    height, width = index.shape
    size = -(-len(data) // parts)
    chunks = [data[i : i + size] for i in range(0, len(data), size)]
    out = b""
    for n, chunk in enumerate(chunks):
        seq = (0x80 if n == 0 else 0) | (0x40 if n == len(chunks) - 1 else 0)
        head = struct.pack(">HBB", object_id, 0, seq)
        if n == 0:
            head += (len(data) + 4).to_bytes(3, "big") + struct.pack(">HH", width, height)
        out += segment(pgs.ODS, ms, head + chunk)
    return out


def end(ms: float) -> bytes:
    return segment(pgs.END, ms, b"")


def wds(ms: float) -> bytes:
    return segment(pgs.WDS, ms, b"\x00")


def text_index(text: str, size: int = 40, colour: int = 1, shear: float = 0.0) -> np.ndarray:
    """`text` drawn as palette indexes (0 transparent, `colour` ink), optionally slanted right by `shear` (tan)."""
    font = ImageFont.load_default(size=size)
    lines = text.split("\n")
    width = int(max(font.getlength(line) for line in lines)) + 20 + int(shear * size * 1.5 * len(lines))
    height = int(size * 1.4) * len(lines) + 10
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    for n, line in enumerate(lines):
        draw.text((10, 5 + n * int(size * 1.4)), line, fill=255, font=font)
    if shear:
        image = image.transform(image.size, Image.Transform.AFFINE, (1, shear, -shear * height, 0, 1, 0))
    return np.where(np.asarray(image) > 127, colour, 0).astype(np.uint8)


def display_set(ms: float, placed: Sequence[tuple[int, np.ndarray, int, int]], *, epoch: bool = False) -> bytes:
    """One display set: (object id, index image, x, y) each, palette 0 white."""
    out = pcs(ms, [(oid, x, y) for oid, _, x, y in placed], epoch_start=epoch) + wds(ms) + pds(ms, {1: WHITE})
    for oid, index, _, _ in placed:
        out += ods(ms, oid, index)
    return out + end(ms)


def clear(ms: float) -> bytes:
    return pcs(ms, []) + wds(ms) + end(ms)


SIGN = text_index("SIGN TEXT")
LINE = text_index("Dialogue line")


@pytest.fixture
def sign_then_dialogue() -> bytes:
    """A sign at the top; a later normal display set adds dialogue under it; then all clears."""
    return (
        display_set(1000, [(0, SIGN, 100, 50)], epoch=True)
        + display_set(2000, [(0, SIGN, 100, 50), (1, LINE, 100, 900)])
        + clear(3000)
    )


# ---------------------------------------------------------------- pgs


def test_rle_round_trip() -> None:
    index = np.zeros((4, 300), dtype=np.uint8)
    index[0, 0:2] = 1  # short run of a colour
    index[1, 10:80] = 2  # long run of a colour
    index[2, 5:200] = 3
    index[3, :] = 0  # long run of transparent
    assert pgs.decode_rle(rle(index), 300, 4) == index.tobytes()


def test_decode_rle_stops_on_truncated_data() -> None:
    assert pgs.decode_rle(b"\x01\x00", 2, 1) == b"\x01\x00"


def test_segments_rejects_a_non_pgs_stream() -> None:
    with pytest.raises(pgs.PgsError, match="no PGS segment"):
        list(pgs.segments(b"XX" + b"\x00" * 20))


def test_segments_rejects_stray_bytes_shorter_than_a_header() -> None:
    with pytest.raises(pgs.PgsError, match="4 stray bytes at byte 0"):
        list(pgs.segments(b"nope"))
    with pytest.raises(pgs.PgsError, match="2 stray bytes at byte 16"):
        list(pgs.segments(segment(0x80, 0, b"abc") + b"PG"))
    assert list(pgs.segments(b"")) == []


def test_segments_rejects_a_cut_short_segment() -> None:
    with pytest.raises(pgs.PgsError, match="cut short"):
        list(pgs.segments(segment(pgs.END, 0, b"abc")[:-1]))


def test_decode_keeps_dialogue_added_under_a_sign(sign_then_dialogue: bytes) -> None:
    comps = pgs.decode(sign_then_dialogue)
    assert [(c.start_ms, c.end_ms) for c in comps] == [(1000, 2000), (2000, 3000)]
    assert comps[0].box[1] == 50 and comps[1].box[3] == 900 + LINE.shape[0]
    assert comps[1].screen == SCREEN


def test_decode_joins_a_multi_part_object() -> None:
    whole = display_set(0, [(0, LINE, 0, 0)], epoch=True) + clear(500)
    split = pcs(0, [(0, 0, 0)], epoch_start=True) + pds(0, {1: WHITE}) + ods(0, 0, LINE, parts=3) + end(0) + clear(500)
    a, b = pgs.decode(whole)[0], pgs.decode(split)[0]
    assert a.image.tobytes() == b.image.tobytes()


def test_decode_crops_an_object() -> None:
    stream = pcs(0, [(0, 10, 20, (5, 0, 30, LINE.shape[0]))], epoch_start=True)
    stream += pds(0, {1: WHITE}) + ods(0, 0, LINE) + end(0)
    (comp,) = pgs.decode(stream)
    assert comp.image.size == (30, LINE.shape[0])
    assert comp.end_ms is None


def test_decode_skips_missing_objects_and_empty_compositions() -> None:
    stream = pcs(0, [(7, 0, 0)], epoch_start=True) + end(0)  # object 7 never defined
    assert pgs.decode(stream) == []


def test_epoch_start_forgets_old_objects() -> None:
    stream = display_set(0, [(0, LINE, 0, 0)], epoch=True)
    stream += pcs(1000, [(0, 0, 0)], epoch_start=True) + end(1000)  # refers to object 0 of the old epoch
    comps = pgs.decode(stream)
    assert [(c.start_ms, c.end_ms) for c in comps] == [(0, 1000)]


def test_ycbcr_to_rgb_clamps() -> None:
    assert pgs.ycbcr_to_rgb(255, 255, 255) == (255, 121, 255)
    assert pgs.ycbcr_to_rgb(128, 128, 128) == (128, 128, 128)
    assert pgs.ycbcr_to_rgb(0, 0, 0) == (0, 135, 0)


# ---------------------------------------------------------------- areas and events


def test_split_separates_top_and_bottom(sign_then_dialogue: bytes) -> None:
    second = pgs.decode(sign_then_dialogue)[1]
    top, bottom = areas.split(second)
    assert (top.position, bottom.position) == (areas.TOP, areas.BOTTOM)
    assert top.top >= 50 and bottom.top >= 900


def test_split_keeps_two_close_lines_together() -> None:
    two = text_index("First line\nSecond line")
    (comp,) = pgs.decode(display_set(0, [(0, two, 0, 900)], epoch=True))
    assert len(areas.split(comp)) == 1


def test_split_of_a_blank_composition() -> None:
    blank = np.zeros((10, 10), dtype=np.uint8)
    (comp,) = pgs.decode(display_set(0, [(0, blank, 0, 0)], epoch=True))
    assert areas.split(comp) == []


def test_area_gap_scales_with_the_screen() -> None:
    assert areas.area_gap(1080) == 40
    assert areas.area_gap(480) == 18


def test_events_follow_a_sign_across_compositions(sign_then_dialogue: bytes) -> None:
    events = areas.events(pgs.decode(sign_then_dialogue))
    assert [(e.start_ms, e.end_ms, e.area.position) for e in events] == [
        (1000, 3000, areas.TOP),
        (2000, 3000, areas.BOTTOM),
    ]


def test_events_split_a_line_repeated_after_a_gap() -> None:
    stream = (
        display_set(0, [(0, LINE, 0, 900)], epoch=True)
        + clear(1000)
        + display_set(1500, [(0, LINE, 0, 900)])
        + clear(2500)
    )
    events = areas.events(pgs.decode(stream))
    assert [(e.start_ms, e.end_ms) for e in events] == [(0, 1000), (1500, 2500)]


def test_events_keep_identical_areas_apart() -> None:
    stream = display_set(0, [(0, LINE, 0, 600), (1, LINE, 0, 900)], epoch=True) + clear(1000)
    events = areas.events(pgs.decode(stream))
    tops = sorted(e.area.top for e in events)
    assert len(events) == 2 and tops[1] - tops[0] == 300


def test_events_end_an_open_composition_after_the_default() -> None:
    events = areas.events(pgs.decode(display_set(0, [(0, LINE, 0, 900)], epoch=True)))
    assert events[0].end_ms == areas.DEFAULT_DURATION_MS


def test_events_end_a_changed_line() -> None:
    other = text_index("Another line")
    stream = display_set(0, [(0, LINE, 0, 900)], epoch=True) + display_set(1000, [(0, other, 0, 900)]) + clear(2000)
    events = areas.events(pgs.decode(stream))
    assert [(e.start_ms, e.end_ms) for e in events] == [(0, 1000), (1000, 2000)]


def test_ink_bitmap_takes_only_bright_pixels() -> None:
    index = np.zeros((20, 40), dtype=np.uint8)
    index[5:10, 5:20] = 1  # white
    index[12:15, 25:35] = 2  # grey: outline, not ink
    stream = pcs(0, [(0, 0, 0)], epoch_start=True) + pds(0, {1: WHITE, 2: GREY}) + ods(0, 0, index) + end(0)
    (comp,) = pgs.decode(stream)
    bitmap = areas.ink_bitmap(comp.image)
    assert bitmap.shape == (5, 15) and (bitmap == 0).all()


def test_ink_bitmap_of_an_area_with_no_ink() -> None:
    stream = pcs(0, [(0, 0, 0)], epoch_start=True) + pds(0, {2: GREY}) + ods(0, 0, np.full((4, 4), 2, np.uint8))
    (comp,) = pgs.decode(stream + end(0))
    assert areas.ink_bitmap(comp.image).size == 0


# ---------------------------------------------------------------- engine


def test_split_lines_attaches_dots_to_the_line_below_and_above() -> None:
    bitmap = np.full((40, 10), 255, dtype=np.uint8)
    bitmap[0:2, 2] = 0  # umlaut dots, above line 1
    bitmap[3:13, :] = 0  # line 1
    bitmap[16:26, :] = 0  # line 2
    bitmap[30:32, 5] = 0  # trailing dots
    lines = engine.split_lines(bitmap)
    assert [line.shape[0] for line in lines] == [13, 16]


def test_add_border_pads_white() -> None:
    padded = engine.add_border(np.zeros((2, 3), dtype=np.uint8))
    assert padded.shape == (10, 11) and padded[0, 0] == 255 and padded[4, 4] == 0


def test_resize_linear_matches_opencv_rounding() -> None:
    row = np.array([[0, 255]], dtype=np.uint8)
    assert engine.resize_linear(row, 4, 1).tolist() == [[0, 64, 191, 255]]
    same = np.arange(12, dtype=np.uint8).reshape(3, 4)
    assert (engine.resize_linear(same, 4, 3) == same).all()


def test_normalise_scales_and_pads() -> None:
    line = np.full((24, 48), 255, dtype=np.uint8)
    out = engine.normalise(line, max_ratio=10)
    assert out.shape == (3, 48, 480)
    assert out[:, :, :96].min() == 1.0 and out[:, :, 96:].max() == 0.0


def test_ctc_decode_collapses_repeats_and_skips_blanks() -> None:
    chars = ["blank", "a", "b", " "]
    steps = [(1, 0.9), (1, 0.8), (0, 0.99), (1, 0.7), (3, 0.1), (3, 0.2), (2, 0.95)]
    preds = np.zeros((len(steps), 4), dtype=np.float32)
    for t, (index, score) in enumerate(steps):
        preds[t, index] = score
    assert engine.ctc_decode(preds, chars) == ("aa b", pytest.approx(0.7))


def test_ctc_decode_of_nothing() -> None:
    assert engine.ctc_decode(np.zeros((3, 2), dtype=np.float32), ["blank", "a"]) == ("", 0.0)


class FakeReader:
    """Reads each line as `L<width>`, with a score from `scores` by call order (default 0.99)."""

    def __init__(self, scores: Sequence[float] = ()) -> None:
        self.scores = list(scores)
        self.calls: list[int] = []

    def read(self, lines: Sequence[engine.Bitmap]) -> list[tuple[str, float]]:
        self.calls.append(len(lines))
        return [(f"L{line.shape[1]}", self.scores[i] if i < len(self.scores) else 0.99) for i, line in enumerate(lines)]


def test_read_areas_joins_lines_and_flags_doubt() -> None:
    two = np.full((30, 20), 255, dtype=np.uint8)
    two[0:10, :] = 0
    two[20:30, 0:10] = 0
    one = np.zeros((10, 5), dtype=np.uint8)
    reader = FakeReader([0.95, 0.80, 0.99])
    readings = engine.read_areas(reader, [two, one, one[:0, :0]])
    assert reader.calls == [3]
    assert readings[0] == engine.Reading("L28\nL28", 0.80, True)  # lines keep the area's width
    assert readings[1] == engine.Reading("L13", 0.99, False)
    assert readings[2] == engine.Reading("", 0.0, True)


class FakeSession:
    def __init__(self, characters: str) -> None:
        self.characters = characters
        self.batches: list[tuple[int, ...]] = []

    def get_modelmeta(self) -> Any:
        meta = type("Meta", (), {})()
        meta.custom_metadata_map = {"character": self.characters} if self.characters else {}
        return meta

    def get_inputs(self) -> list[Any]:
        return [type("Input", (), {"name": "x"})()]

    def run(self, _outputs: None, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        batch = feed["x"]
        self.batches.append(batch.shape)
        preds = np.zeros((batch.shape[0], 4, 4), dtype=np.float32)
        for row in range(batch.shape[0]):
            # one character per line: "a" for a line wider than 200 px after scaling, else "b"
            ink = int((batch[row, 0] < 0).any(axis=0).sum())
            preds[row, 0, 1 if ink > 200 else 2] = 0.9
        return [preds]


def fake_onnx(monkeypatch: pytest.MonkeyPatch, characters: str) -> FakeSession:
    import onnxruntime

    session = FakeSession(characters)
    monkeypatch.setattr(onnxruntime, "InferenceSession", lambda *a, **k: session)
    return session


def test_onnx_reader_batches_by_width_and_keeps_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = fake_onnx(monkeypatch, "a\nb")
    reader = engine.OnnxLineReader(tmp_path / "m.onnx", batch=2)
    assert reader.characters == ["blank", "a", "b", " "]
    wide, narrow = np.zeros((48, 400), np.uint8), np.zeros((48, 100), np.uint8)
    assert reader.read([wide, narrow, wide]) == [("a", pytest.approx(0.9)), ("b", pytest.approx(0.9))] + [
        ("a", pytest.approx(0.9))
    ]
    assert [shape[0] for shape in session.batches] == [2, 1]


def test_onnx_reader_refuses_a_model_without_characters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_onnx(monkeypatch, "")
    with pytest.raises(ValueError, match="no character list"):
        engine.OnnxLineReader(tmp_path / "m.onnx")


def test_model_path_defaults_to_the_bundled_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine.MODEL_ENV, raising=False)
    assert engine.model_path() == Path(BUNDLED_MODEL.path)
    monkeypatch.setenv(engine.MODEL_ENV, "/tmp/other.onnx")
    assert engine.model_path() == Path("/tmp/other.onnx")


# ---------------------------------------------------------------- italic


def area_image(index: np.ndarray) -> Image.Image:
    (comp,) = pgs.decode(display_set(0, [(0, index, 0, 900)], epoch=True))
    return comp.image


def test_upright_text_is_not_italic() -> None:
    image = area_image(text_index("Upright words here"))
    assert abs(italic.slant(image)) < italic.ITALIC_DEGREES
    assert not italic.is_italic(image)


def test_slanted_text_is_italic() -> None:
    image = area_image(text_index("Slanted words here", shear=0.25))  # about 14 degrees
    assert italic.slant(image) >= italic.ITALIC_DEGREES
    assert italic.is_italic(image)


def test_lightly_slanted_text_is_italic() -> None:
    # Hanako-kun's italic leans about 7 degrees, half as much as ACCA's
    assert italic.is_italic(area_image(text_index("Lightly slanted words", shear=0.125)))


def centred(*lines: np.ndarray) -> np.ndarray:
    width = max(line.shape[1] for line in lines)
    padded = []
    for line in lines:
        left = (width - line.shape[1]) // 2
        padded.append(np.pad(line, ((0, 0), (left, width - line.shape[1] - left))))
    return np.vstack(padded)


def test_each_line_is_measured_on_its_own() -> None:
    upright = centred(text_index("Short"), text_index("A much longer second line"))
    slanted = centred(text_index("Short", shear=0.125), text_index("A much longer second line", shear=0.125))
    assert not italic.is_italic(area_image(upright))
    assert italic.is_italic(area_image(slanted))


def test_an_area_with_no_fill_is_upright() -> None:
    assert italic.slant(Image.new("RGBA", (40, 20), (0, 0, 0, 0))) == 0.0


# ---------------------------------------------------------------- srt


def test_timestamp() -> None:
    assert srt.timestamp(3_723_004) == "01:02:03,004"
    assert srt.timestamp(-5) == "00:00:00,000"


def test_cue_text_marks_top_and_italic_per_line() -> None:
    cue = srt.Cue(0, 1, "one\ntwo", top=True, italic=True)
    assert srt.cue_text(cue) == "{\\an8}<i>one</i>\n<i>two</i>"


def test_render_numbers_cues_and_skips_empty_ones() -> None:
    text = srt.render([srt.Cue(0, 1500, "Hi"), srt.Cue(500, 900, ""), srt.Cue(1000, 2000, "Sign", top=True)])
    assert text == "1\n00:00:00,000 --> 00:00:01,500\nHi\n\n2\n00:00:01,000 --> 00:00:02,000\n{\\an8}Sign\n"


# ---------------------------------------------------------------- run


def test_srt_path_for_keeps_the_language_parts() -> None:
    assert run.srt_path_for(Path("/m/Show - S01E01.eng.2.sup")) == Path("/m/Show - S01E01.eng.2.srt")


def test_convert_writes_the_srt_through_inprogress(sign_then_dialogue: bytes, tmp_path: Path) -> None:
    sup = tmp_path / "Video.eng.sup"
    sup.write_bytes(sign_then_dialogue)
    written, stats = run.convert(sup, FakeReader())
    assert written == tmp_path / "Video.eng.srt"
    assert not list(tmp_path.glob("*.arm-inprogress"))
    body = written.read_text(encoding="utf-8")
    assert body.startswith("1\n00:00:01,000 --> 00:00:03,000\n{\\an8}L")
    assert "2\n00:00:02,000 --> 00:00:03,000\nL" in body
    assert (stats.compositions, stats.events, stats.unique_areas, stats.cues) == (2, 2, 2, 2)
    assert (stats.doubtful, stats.unread) == (0, 0)


def test_convert_leaves_out_unread_areas(sign_then_dialogue: bytes, tmp_path: Path) -> None:
    class Blank(FakeReader):
        def read(self, lines: Sequence[engine.Bitmap]) -> list[tuple[str, float]]:
            return [("", 0.0)] * len(lines)

    sup = tmp_path / "v.eng.sup"
    sup.write_bytes(sign_then_dialogue)
    written, stats = run.convert(sup, Blank(), output=tmp_path / "out.srt")
    assert written == tmp_path / "out.srt" and written.read_text() == ""
    assert (stats.cues, stats.unread) == (0, 2)


def test_cli_converts_folders_skips_existing_and_reports_errors(
    sign_then_dialogue: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(engine, "OnnxLineReader", lambda threads=1: FakeReader())
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.eng.sup").write_bytes(sign_then_dialogue)
    (tmp_path / "b.eng.sup").write_bytes(sign_then_dialogue)
    (tmp_path / "b.eng.srt").write_text("old")
    (tmp_path / "bad.eng.sup").write_bytes(b"nope" * 10)

    assert cli.main([str(tmp_path / "s"), str(tmp_path / "b.eng.sup"), str(tmp_path / "bad.eng.sup")]) == 1
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[0]["srt"].endswith("a.eng.srt") and out[0]["cues"] == 2
    assert out[1]["skipped"] == "srt exists"
    assert out[2]["error"].startswith("PgsError")
    assert (tmp_path / "b.eng.srt").read_text() == "old"

    assert cli.main(["--overwrite", str(tmp_path / "b.eng.sup")]) == 0
    assert (tmp_path / "b.eng.srt").read_text() != "old"


# ---------------------------------------------------------------- check


def test_load_reference_reads_lines_and_collapses_spaces(tmp_path: Path) -> None:
    ref = tmp_path / "ref.tsv"
    ref.write_text('# comment\n\neng\t3\t1\t1\t"A"   "B" / second\neng.2\t0\t0\t0\tplain\n', encoding="utf-8")
    assert check.load_reference(ref) == [
        check.Sample("eng", 3, 1, True, '"A" "B"\nsecond'),
        check.Sample("eng.2", 0, 0, False, "plain"),
    ]


def test_track_of_prefers_the_longest_match() -> None:
    tracks = {"eng", "eng.2"}
    assert check.track_of(Path("x.eng.2.sup"), tracks) == "eng.2"
    assert check.track_of(Path("x.eng.sup"), tracks) == "eng"
    assert check.track_of(Path("x.fra.sup"), tracks) is None


def test_edit_distance() -> None:
    assert check.edit_distance("kitten", "sitting") == 3
    assert check.edit_distance("", "ab") == 2


def test_score_and_report() -> None:
    samples = [
        check.Sample("eng", 0, 0, False, "hello"),
        check.Sample("eng", 1, 0, True, "song"),
        check.Sample("eng", 2, 0, False, "gone"),
    ]
    found = {("eng", 0, 0): ("hello", True), ("eng", 1, 0): ("sung", True)}
    result = check.score(samples, found)
    assert (result.exact, result.missing, result.edits, result.characters) == (1, 1, 5, 13)
    assert (result.italic_found, result.italic_total, result.upright_marked) == (1, 1, 1)
    assert result.cer == pytest.approx(5 / 13)
    text = check.report(result)
    assert "exact 1" in text and "want 'song' got 'sung'" in text and "read as italic: 'hello'" in text
    assert check.score([], {}).cer == 0.0


def test_check_main_reads_the_referenced_areas(
    sign_then_dialogue: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(engine, "OnnxLineReader", lambda threads=1: FakeReader())
    sup = tmp_path / "v.eng.sup"
    sup.write_bytes(sign_then_dialogue)
    (tmp_path / "v.fra.sup").write_bytes(sign_then_dialogue)  # no reference track: skipped
    width = int(areas.ink_bitmap(areas.split(pgs.decode(sign_then_dialogue)[1])[1].image).shape[1]) + 8
    ref = tmp_path / "ref.tsv"
    ref.write_text(f"eng\t1\t1\t0\tL{width}\neng\t1\t5\t0\tnone\neng\t9\t0\t0\tnone\n", encoding="utf-8")
    assert check.main([str(ref), str(sup), str(tmp_path / "v.fra.sup")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("lines 3, exact 1, missing 2")


# ---------------------------------------------------------------- the bundled model is listed consistently


def test_bundled_model_matches_the_dockerfile_and_notice() -> None:
    service = Path(__file__).resolve().parents[1]
    dockerfile = (service / "Dockerfile").read_text()
    notice = (service / "models" / f"{BUNDLED_MODEL.name}.NOTICE").read_text()
    assert f"ARG OCR_MODEL_SHA256={BUNDLED_MODEL.sha256}" in dockerfile
    assert f"ARG OCR_MODEL_URL={BUNDLED_MODEL.source}" in dockerfile
    assert f"/opt/arm/models/{BUNDLED_MODEL.name}.onnx" == BUNDLED_MODEL.path
    assert BUNDLED_MODEL.sha256 in notice and f"{BUNDLED_MODEL.size:,}" in notice and BUNDLED_MODEL.source in notice


# ---------------------------------------------------------------- the transcode task

# The reader process with a fake engine: every line reads `L<width>`.
FAKE_READER_SCRIPT = """
import sys
from arm_transcode.subtitle_ocr import __main__ as cli, engine

class Fake:
    def read(self, lines):
        return [(f"L{line.shape[1]}", 0.99) for line in lines]

engine.OnnxLineReader = lambda threads=1: Fake()
sys.exit(cli.main(sys.argv[1:]))
"""


@pytest.fixture
def fake_reader_process(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    script = tmp_path_factory.mktemp("reader") / "fake_reader.py"
    script.write_text(FAKE_READER_SCRIPT)
    commands: list[list[str]] = []
    real_command = task.reader_command

    def command(sidecar: Path, threads: int = task.THREADS) -> list[str]:
        real = real_command(sidecar, threads)
        commands.append(real)
        return [sys.executable, str(script), *real[3:]]

    monkeypatch.setattr(task, "reader_command", command)
    return commands


def _script(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    monkeypatch.setattr(task, "reader_command", lambda sidecar, threads=1: [sys.executable, "-c", code, str(sidecar)])


async def test_task_reads_each_sidecar_in_its_own_process(
    sign_then_dialogue: bytes, tmp_path: Path, fake_reader_process: list[list[str]]
) -> None:
    sups = [tmp_path / "v.eng.sup", tmp_path / "v.eng.2.sup"]
    for sup in sups:
        sup.write_bytes(sign_then_dialogue)
    (tmp_path / "v.eng.srt").write_text("old")  # replaced: the task decided to write it
    progress: list[int] = []
    written = await task.make_text_subtitles(sups, on_progress=progress.append)
    assert written == [tmp_path / "v.eng.srt", tmp_path / "v.eng.2.srt"]
    assert all("{\\an8}" in p.read_text(encoding="utf-8") for p in written)
    assert progress == [0, 50, 100]
    assert fake_reader_process[0][1:] == [
        "-m",
        "arm_transcode.subtitle_ocr",
        "--overwrite",
        "--threads",
        "1",
        str(sups[0]),
    ]
    assert not list(tmp_path.glob("*.arm-inprogress"))


async def test_task_failure_keeps_the_finished_srt(
    sign_then_dialogue: bytes, tmp_path: Path, fake_reader_process: list[list[str]]
) -> None:
    good, bad, never = tmp_path / "v.eng.sup", tmp_path / "v.fra.sup", tmp_path / "v.spa.sup"
    good.write_bytes(sign_then_dialogue)
    bad.write_bytes(b"nope" * 10)
    never.write_bytes(sign_then_dialogue)
    with pytest.raises(task.SubtitleOcrFailed, match=r"v\.fra\.sup: subtitle OCR failed \(rc=1\): PgsError") as caught:
        await task.make_text_subtitles([good, bad, never], on_progress=lambda pct: None)
    assert caught.value.written == [tmp_path / "v.eng.srt"]
    assert (tmp_path / "v.eng.srt").exists() and not (tmp_path / "v.spa.srt").exists()


async def test_task_reports_the_stderr_tail_of_a_reader_that_printed_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, "import sys; print('not json'); sys.stderr.write('a\\nboom\\n'); sys.exit(3)")
    with pytest.raises(RuntimeError, match=r"x\.sup: subtitle OCR failed \(rc=3\): a\nboom"):
        await task.read_sidecar(tmp_path / "x.sup")


async def test_task_refuses_a_report_without_an_srt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _script(monkeypatch, "import json; print(json.dumps({'sidecar': 'x', 'skipped': 'srt exists'}))")
    with pytest.raises(RuntimeError, match=r"rc=0"):
        await task.read_sidecar(tmp_path / "x.sup")


async def test_task_cancel_kills_the_reader_and_removes_its_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = tmp_path / "x.srt.arm-inprogress"
    _script(monkeypatch, f"import pathlib, time; pathlib.Path({str(partial)!r}).write_text('half'); time.sleep(60)")
    reading = asyncio.ensure_future(task.read_sidecar(tmp_path / "x.sup"))
    for _ in range(200):
        if partial.exists():
            break
        await asyncio.sleep(0.05)
    assert partial.exists()
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reading, timeout=10)
    assert not partial.exists()


async def test_task_cancel_removes_the_srt_already_written(tmp_path: Path) -> None:
    first = tmp_path / "v.eng.srt"
    blocked = asyncio.Event()

    async def reader(sidecar: Path) -> Path:
        if sidecar.name == "v.eng.sup":
            first.write_text("1")
            return first
        blocked.set()
        await asyncio.sleep(60)
        raise AssertionError("not cancelled")

    making = asyncio.ensure_future(
        task.make_text_subtitles(
            [tmp_path / "v.eng.sup", tmp_path / "v.fra.sup"], on_progress=lambda pct: None, reader=reader
        )
    )
    await blocked.wait()
    making.cancel()
    with pytest.raises(asyncio.CancelledError):
        await making
    assert not first.exists()


async def test_main_runs_a_subtitle_ocr_task(
    sign_then_dialogue: bytes, tmp_path: Path, fake_reader_process: list[list[str]]
) -> None:
    import arm_transcode.main as m
    from arm_transcode.heartbeat import ProgressState

    folder = tmp_path / "Show"
    folder.mkdir()
    (folder / "Show - S01E01.eng.sup").write_bytes(sign_then_dialogue)
    state = ProgressState()
    result = await m._run_subtitle_ocr(tmp_path, ["Show/Show - S01E01.eng.sup"], state)
    srt_file = folder / "Show - S01E01.eng.srt"
    assert result.sidecars == [srt_file]
    assert result.size == srt_file.stat().st_size > 0
    assert (state.pct, state.current_pass) == (100, "text subtitles")
