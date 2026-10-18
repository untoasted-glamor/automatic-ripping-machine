import { describe, it, expect } from 'vitest';
import { formatDuration, trackDuration } from './duration';

describe('formatDuration', () => {
	it('uses M:SS under an hour', () => {
		expect(formatDuration(0)).toBe('0:00');
		expect(formatDuration(59)).toBe('0:59');
		expect(formatDuration(1452)).toBe('24:12');
		expect(formatDuration(3599)).toBe('59:59');
	});

	it('uses H:MM:SS from an hour up', () => {
		expect(formatDuration(3600)).toBe('1:00:00');
		expect(formatDuration(3661)).toBe('1:01:01');
		expect(formatDuration(13078)).toBe('3:37:58');
	});

	it('rounds fractional seconds and clamps negatives to zero', () => {
		expect(formatDuration(59.6)).toBe('1:00');
		expect(formatDuration(-5)).toBe('0:00');
	});
});

describe('trackDuration', () => {
	it('prefers the post-rip actual', () => {
		expect(trackDuration(13078, 13000)).toEqual({ label: '3:37:58', estimated: false });
	});

	it('falls back to the scan estimate, flagged as estimated', () => {
		expect(trackDuration(null, 1452)).toEqual({ label: '24:12', estimated: true });
		expect(trackDuration(undefined, 0)).toEqual({ label: '0:00', estimated: true });
	});

	it('is null when neither is known', () => {
		expect(trackDuration(null, null)).toBeNull();
		expect(trackDuration(undefined, undefined)).toBeNull();
	});
});
