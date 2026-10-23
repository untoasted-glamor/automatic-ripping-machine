import { describe, it, expect } from 'vitest';
import { createTrack } from '$lib/components/__fixtures__/job';
import { keepLockedReason, keepSummary, trackRipStatus } from './track-keep';

const GB = 1024 ** 3;

describe('keepSummary', () => {
	it('sums the kept tracks, preferring ripped values over scan estimates', () => {
		const tracks = [
			createTrack({ id: 'a', size_bytes: 2 * GB, duration_seconds: 1500 }),
			createTrack({
				id: 'b',
				size_bytes: null,
				expected_size_bytes: GB,
				duration_seconds: null,
				expected_duration_seconds: 1500
			}),
			createTrack({ id: 'c', excluded: true, size_bytes: 40 * GB, duration_seconds: 9000 })
		];
		expect(keepSummary(tracks)).toBe('Keeping 2 of 3 titles, ~3 GB, 50:00');
	});

	it('leaves out what nothing says and takes a noun for CDs', () => {
		const tracks = [createTrack({ size_bytes: null, expected_size_bytes: null, duration_seconds: null })];
		expect(keepSummary(tracks, 'tracks')).toBe('Keeping 1 of 1 tracks');
	});
});

describe('keepLockedReason', () => {
	it('locks the boxes only while ripping', () => {
		expect(keepLockedReason('ripping')).toBe("Titles can't be changed while the disc is ripping");
		expect(keepLockedReason('awaiting_review')).toBeNull();
		expect(keepLockedReason('ripped')).toBeNull();
	});
});

describe('trackRipStatus', () => {
	it('shows a track left out before the rip as skipped once the rip started', () => {
		const left = createTrack({ excluded: true, status: 'queued' });
		expect(trackRipStatus(left, true)).toBe('skipped');
		expect(trackRipStatus(left, false)).toBe('queued');
		expect(trackRipStatus(createTrack({ excluded: true, status: 'done' }), true)).toBe('done');
		expect(trackRipStatus(createTrack({ excluded: false, status: 'queued' }), true)).toBe('queued');
	});
});
