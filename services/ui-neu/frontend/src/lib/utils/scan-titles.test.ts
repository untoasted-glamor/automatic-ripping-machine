import { describe, it, expect } from 'vitest';
import { listedScanTitles } from './scan-titles';

describe('listedScanTitles', () => {
	it('drops titles below the scan minimum and keeps unknown durations', () => {
		const scan = {
			min_length_seconds: 120,
			titles: [
				{ index: 0, duration_seconds: 898 },
				{ index: 1, duration_seconds: 119 },
				{ index: 2, duration_seconds: 120 },
				{ index: 3, duration_seconds: null }
			]
		};
		expect(listedScanTitles(scan).map((t) => t.index)).toEqual([0, 2, 3]);
	});

	it('shows every title of a scan without a minimum', () => {
		const scan = { titles: [{ index: 0, duration_seconds: 10 }, { index: 1 }] };
		expect(listedScanTitles(scan).map((t) => t.index)).toEqual([0, 1]);
	});

	it('returns nothing for a missing or malformed scan', () => {
		expect(listedScanTitles(undefined)).toEqual([]);
		expect(listedScanTitles({ raw: {} })).toEqual([]);
	});
});
