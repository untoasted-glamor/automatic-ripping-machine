import { describe, it, expect } from 'vitest';
import { activeTranscodesLabel, encodeTasks, isSubtitleOcr } from './transcode-kind';

describe('transcode task kind', () => {
	const encode = { kind: 'encode' as const };
	const legacy = {}; // a view from before the kind field: an encode
	const ocr = { kind: 'subtitle_ocr' as const };

	it('tells subtitle OCR tasks from encodes', () => {
		expect(isSubtitleOcr(ocr)).toBe(true);
		expect(isSubtitleOcr(encode)).toBe(false);
		expect(isSubtitleOcr(legacy)).toBe(false);
		expect(encodeTasks([encode, ocr, legacy])).toEqual([encode, legacy]);
	});

	it('labels the active section by encodes, then text subtitles', () => {
		expect(activeTranscodesLabel([encode, legacy])).toBe('TRANSCODING - 2 ACTIVE');
		expect(activeTranscodesLabel([encode, ocr])).toBe('TRANSCODING - 1 ACTIVE - 1 TEXT SUBTITLES');
		expect(activeTranscodesLabel([ocr, ocr])).toBe('TRANSCODING - 2 TEXT SUBTITLES');
		expect(activeTranscodesLabel([])).toBe('TRANSCODING - 0 ACTIVE');
	});
});
