// Helpers for reading the transcoder stats payload.
import type { TranscodeStatsView } from '$lib/types/api.gen';

/** Number of transcode tasks in `status`; 0 when the status is absent. */
export function taskCount(stats: TranscodeStatsView, status: string): number {
	return stats.tasks_by_status?.[status] ?? 0;
}

/** Number of subtitle OCR tasks in `status` (counted apart from the encodes); 0 when the status is absent. */
export function ocrTaskCount(stats: TranscodeStatsView, status: string): number {
	return stats.subtitle_ocr_tasks_by_status?.[status] ?? 0;
}
