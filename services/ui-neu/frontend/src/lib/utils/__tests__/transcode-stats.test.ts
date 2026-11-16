import { describe, it, expect } from 'vitest';
import { ocrTaskCount, taskCount } from '$lib/utils/transcode-stats';
import type { TranscodeStatsView } from '$lib/types/api.gen';

describe('taskCount', () => {
	it('returns the count for a status present in tasks_by_status', () => {
		const stats = { tasks_by_status: { queued: 3 } } as unknown as TranscodeStatsView;
		expect(taskCount(stats, 'queued')).toBe(3);
	});

	it('returns 0 for a status missing from tasks_by_status', () => {
		const stats = { tasks_by_status: { queued: 3 } } as unknown as TranscodeStatsView;
		expect(taskCount(stats, 'failed')).toBe(0);
	});

	it('returns 0 when tasks_by_status is missing', () => {
		const stats = {} as unknown as TranscodeStatsView;
		expect(taskCount(stats, 'queued')).toBe(0);
	});
});

describe('ocrTaskCount', () => {
	it('counts subtitle OCR tasks apart from the encodes', () => {
		const stats = {
			tasks_by_status: { queued: 3 },
			subtitle_ocr_tasks_by_status: { queued: 1 }
		} as unknown as TranscodeStatsView;
		expect(ocrTaskCount(stats, 'queued')).toBe(1);
	});

	it('returns 0 when subtitle_ocr_tasks_by_status is missing', () => {
		const stats = { tasks_by_status: { queued: 3 } } as unknown as TranscodeStatsView;
		expect(ocrTaskCount(stats, 'queued')).toBe(0);
	});
});
