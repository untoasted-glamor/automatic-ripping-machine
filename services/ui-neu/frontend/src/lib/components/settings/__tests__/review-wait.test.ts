import { describe, it, expect } from 'vitest';
import { reviewWaitError, reviewWaitValue } from '../review-wait';

describe('reviewWaitError', () => {
	it('never errors while the gate is off or in mandatory mode', () => {
		expect(reviewWaitError(false, false, '')).toBeNull();
		expect(reviewWaitError(true, true, '')).toBeNull();
	});

	it('requires a positive whole number in timed mode', () => {
		expect(reviewWaitError(true, false, ' ')).toMatch(/Enter a countdown/);
		expect(reviewWaitError(true, false, '1.5')).toMatch(/whole number/);
		expect(reviewWaitError(true, false, '-3')).toMatch(/whole number/);
		expect(reviewWaitError(true, false, 'abc')).toMatch(/whole number/);
		expect(reviewWaitError(true, false, ' 45 ')).toBeNull();
	});
});

describe('reviewWaitValue', () => {
	it('is null in mandatory mode', () => {
		expect(reviewWaitValue(true, '60')).toBeNull();
	});

	it('parses a valid draft, else undefined', () => {
		expect(reviewWaitValue(false, ' 120 ')).toBe(120);
		expect(reviewWaitValue(false, '')).toBeUndefined();
		expect(reviewWaitValue(false, '0')).toBeUndefined();
	});
});
