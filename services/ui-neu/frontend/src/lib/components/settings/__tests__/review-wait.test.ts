import { describe, it, expect } from 'vitest';
import { isValidWaitSeconds, reviewWaitError, waitSecondsOrDefault } from '../review-wait';

describe('reviewWaitError', () => {
	it('accepts a whole number of seconds, 0 included', () => {
		expect(reviewWaitError(0)).toBeNull();
		expect(reviewWaitError(45)).toBeNull();
	});

	it('flags an empty box', () => {
		expect(reviewWaitError(null)).toMatch(/Enter a countdown/);
		expect(reviewWaitError(undefined)).toMatch(/Enter a countdown/);
		expect(reviewWaitError('')).toMatch(/Enter a countdown/);
	});

	it('flags a negative, fractional or non-numeric value', () => {
		expect(reviewWaitError(-3)).toMatch(/whole number/);
		expect(reviewWaitError(1.5)).toMatch(/whole number/);
		expect(reviewWaitError(Number.NaN)).toMatch(/whole number/);
		expect(reviewWaitError('60')).toMatch(/whole number/);
	});
});

describe('waitSecondsOrDefault', () => {
	it('takes the first valid candidate', () => {
		expect(waitSecondsOrDefault(null, 90, 30)).toBe(90);
		expect(waitSecondsOrDefault(0)).toBe(0);
	});

	it('falls back to 60 s', () => {
		expect(waitSecondsOrDefault()).toBe(60);
		expect(waitSecondsOrDefault(null, -1, 'x')).toBe(60);
	});

	it('isValidWaitSeconds narrows to a number', () => {
		expect(isValidWaitSeconds(12)).toBe(true);
		expect(isValidWaitSeconds('12')).toBe(false);
	});
});
