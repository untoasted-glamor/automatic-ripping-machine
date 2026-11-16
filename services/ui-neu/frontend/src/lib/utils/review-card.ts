// The review card's text, built from the job's state.

/** The job states that decide the review card's status label. */
export interface StatusLabelFlags {
	/** The disc was ripped before. */
	previouslyRipped: boolean;
	/** The disc is backed up and its titles are being chosen (fork). */
	choosingTitles?: boolean;
	/** The disc menu has been read, so the titles can be chosen (fork). */
	titlesOpen?: boolean;
	/** The job is held for an explicit confirm (fork). */
	requiresConfirm?: boolean;
	/** An identity suggestion is waiting to be confirmed. */
	suggestion: boolean;
	/** At the review gate, held for an explicit confirm with no countdown (fork). */
	awaitingConfirm?: boolean;
	/** The job is at the review gate. */
	reviewGate: boolean;
	/** The rip is done and a session can be applied. */
	postRip: boolean;
}

/** The status bar's label; the first matching state wins. */
export function reviewStatusLabel(flags: StatusLabelFlags): string {
	if (flags.previouslyRipped) return 'Already ripped: Rip again or Discard';
	if (flags.choosingTitles) {
		if (!flags.titlesOpen) return 'Backed up: Reading the disc menu';
		return flags.requiresConfirm ? 'Backed up: Choose titles & Confirm' : 'Backed up: Choose titles';
	}
	if (flags.suggestion) return 'Confirm the title: use it or search';
	if (flags.awaitingConfirm) return 'Ready: Review & Confirm';
	if (flags.reviewGate) return 'Ready: Review & Start';
	if (flags.postRip) return 'Ripped: Apply Session';
	return 'Awaiting Review';
}
