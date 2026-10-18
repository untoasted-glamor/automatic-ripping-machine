// Review-gate sub-mode helpers for the settings form (ReviewWaitField).
//
// `review_requires_confirm` picks the sub-mode: true = a held disc waits in
// awaiting_review until the operator presses Start rip (no countdown); false
// = it auto-starts after `manual_wait_seconds`. The countdown column is NOT
// NULL and 0 is a valid choice (start at once), so the box must always hold a
// whole number of seconds, even while hidden in confirm mode.

export const DEFAULT_WAIT_SECONDS = 60;

/** Whether `value` is a countdown the backend accepts: a whole number >= 0. */
export function isValidWaitSeconds(value: unknown): value is number {
	return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

/** Inline message for the countdown box, or null when it can be saved. */
export function reviewWaitError(value: unknown): string | null {
	if (value === null || value === undefined || value === '') return 'Enter a countdown in seconds.';
	if (!isValidWaitSeconds(value)) return 'Countdown must be a whole number of seconds, 0 or more.';
	return null;
}

/** The first valid countdown among `candidates`, else the 60 s default. */
export function waitSecondsOrDefault(...candidates: unknown[]): number {
	return candidates.find(isValidWaitSeconds) ?? DEFAULT_WAIT_SECONDS;
}
