// Review-gate countdown (`manual_wait_seconds`) helpers for the settings form.
//
// The field is nullable on the wire: `null` = mandatory confirm (a held disc
// waits in awaiting_review until the operator clicks Start), a positive integer
// = the timed countdown before the rip auto-starts. The form edits it through a
// "Require manual confirm" toggle plus a text DRAFT, so a box cleared mid-retype
// ("60" -> "" -> "120") is caught locally instead of PATCHing "" and failing
// validation with a generic error.

export const DEFAULT_WAIT_SECONDS = 60;

/** Validation message for the countdown draft, or null when it can be saved. */
export function reviewWaitError(
	holdForReview: boolean,
	mandatory: boolean,
	draft: string
): string | null {
	if (!holdForReview || mandatory) return null;
	const raw = draft.trim();
	if (raw === '') return 'Enter a countdown in seconds (or tick "Require manual confirm").';
	const n = Number(raw);
	if (!Number.isInteger(n) || n < 1) return 'Countdown must be a whole number of seconds, at least 1.';
	return null;
}

/**
 * The wire value for the countdown: null in mandatory mode, the parsed number
 * otherwise, or undefined when the draft isn't a valid countdown (caller skips it).
 */
export function reviewWaitValue(mandatory: boolean, draft: string): number | null | undefined {
	if (mandatory) return null;
	const n = Number(draft.trim());
	return draft.trim() !== '' && Number.isInteger(n) && n >= 1 ? n : undefined;
}
