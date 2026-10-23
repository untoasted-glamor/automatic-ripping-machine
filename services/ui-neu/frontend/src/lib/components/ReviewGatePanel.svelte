<script lang="ts">
	import type { JobView } from '$lib/types/api.gen';
	import { pauseWaitingJob, startWaitingJob } from '$lib/api/jobs';
	import { previouslyRippedJobId } from '$lib/utils/job-status';
	import { isAdmin } from '$lib/stores/auth';
	import ReviewWaitStatus from './ReviewWaitStatus.svelte';

	// The job page's review-gate panel for a disc held in awaiting_review: how
	// it will get to the rip (confirm or countdown, via ReviewWaitStatus) and
	// the Start action, so a held disc can be released from its own page as
	// well as from the dashboard card.
	interface Props {
		job: JobView;
		/** Mandatory-confirm mode (config review_requires_confirm). */
		requiresConfirm: boolean;
		/** Countdown length (config manual_wait_seconds). */
		waitSeconds: number;
		/** Global ripping pause. */
		paused?: boolean;
		/** Kept tracks, when the job has track rows: none kept, nothing to rip. */
		keptCount?: number | null;
		/** The job changed (started, or its countdown paused): reload it. */
		onchanged?: () => void;
	}

	let { job, requiresConfirm, waitSeconds, paused = false, keptCount = null, onchanged }: Props = $props();

	let busy = $state(false);
	let error = $state<string | null>(null);

	// A previously-ripped hold waits for Rip again whatever the mode.
	const previousRip = $derived(previouslyRippedJobId(job) !== null);
	const awaitingConfirm = $derived(requiresConfirm && !previousRip);
	const message = $derived(
		previousRip
			? 'ARM ripped this disc before. Rip it again, or abandon this job.'
			: requiresConfirm
				? "This disc won't start ripping until you confirm it. Check the title and tracks first."
				: paused
					? 'Ripping is paused, so the countdown is on hold.'
					: job.manual_pause
						? 'The countdown is paused for this disc.'
						: 'The rip starts by itself when the countdown ends.'
	);
	const nothingKept = $derived(keptCount === 0 && !previousRip);
	const startLabel = $derived(previousRip ? 'Rip again' : awaitingConfirm ? 'Confirm & start rip' : 'Start rip now');

	async function run(action: () => Promise<unknown>, failure: string) {
		busy = true;
		error = null;
		try {
			await action();
			onchanged?.();
		} catch (e) {
			error = e instanceof Error ? e.message : failure;
		} finally {
			busy = false;
		}
	}

	const start = () => run(() => startWaitingJob(job.id), 'Failed to start the rip');
	const setPause = (p: boolean) => run(() => pauseWaitingJob(job.id, p), 'Failed to update the pause');
</script>

<section class="review-gate-panel stack" data-testid="review-gate-panel">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h2 class="review-gate-panel-title">{previousRip ? 'Already ripped' : 'Held for review'}</h2>
		{#if !previousRip}
			<ReviewWaitStatus
				{job}
				{requiresConfirm}
				{waitSeconds}
				{paused}
				onpause={!$isAdmin || paused || busy ? undefined : () => setPause(true)}
				onresume={!$isAdmin || paused || busy ? undefined : () => setPause(false)}
			/>
		{/if}
	</div>
	<p class="review-gate-panel-text">{message}</p>
	{#if nothingKept}
		<p class="review-gate-panel-error" data-testid="review-gate-nothing-kept">
			Keep at least one title to start the rip.
		</p>
	{/if}
	{#if error}
		<p class="review-gate-panel-error" role="alert">{error}</p>
	{/if}
	{#if $isAdmin}
		<div>
			<button
				type="button"
				class="btn btn-primary"
				data-testid="review-gate-start"
				onclick={start}
				disabled={busy || nothingKept}
			>
				{busy ? 'Working...' : startLabel}
			</button>
		</div>
	{/if}
</section>

<style>
	/* same card as the job page's lifecycle / rip-progress sections. */
	.review-gate-panel {
		gap: 0.5rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		background: var(--color-surface);
		padding: 0.75rem 1rem;
		box-shadow: var(--shadow-1);
	}
	.review-gate-panel-title {
		font-size: 1.125rem;
		line-height: 1.75rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.review-gate-panel-text {
		font-size: 0.875rem;
		color: var(--color-text-muted);
	}
	.review-gate-panel-error {
		font-size: 0.875rem;
		color: var(--color-danger);
	}
</style>
