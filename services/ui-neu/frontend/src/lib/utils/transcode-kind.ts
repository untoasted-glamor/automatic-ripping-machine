import type { TranscodeTaskKind } from '$lib/types/api.gen';

/** Shown on a subtitle OCR task: it writes `.srt` files beside an encode's video. */
export const SUBTITLE_OCR_LABEL = 'Text subtitles';

/**
 * A subtitle OCR task reads the image subtitles a finished encode wrote and
 * writes text subtitles; it shares that encode's track and output path. The
 * transcode counts, worker slots and time estimates are about encodes only.
 */
export function isSubtitleOcr(task: { kind?: TranscodeTaskKind | null }): boolean {
	return task.kind === 'subtitle_ocr';
}

export function encodeTasks<T extends { kind?: TranscodeTaskKind | null }>(tasks: T[]): T[] {
	return tasks.filter((t) => !isSubtitleOcr(t));
}

/** The dashboard's active-transcodes section label: encodes, then OCR tasks. */
export function activeTranscodesLabel(tasks: { kind?: TranscodeTaskKind | null }[]): string {
	const encodes = encodeTasks(tasks).length;
	const ocr = tasks.length - encodes;
	const parts = [];
	if (encodes > 0 || ocr === 0) parts.push(`${encodes} ACTIVE`);
	if (ocr > 0) parts.push(`${ocr} ${SUBTITLE_OCR_LABEL.toUpperCase()}`);
	return `TRANSCODING - ${parts.join(' - ')}`;
}
