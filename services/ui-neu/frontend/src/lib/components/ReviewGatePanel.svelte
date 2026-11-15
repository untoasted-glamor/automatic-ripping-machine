<script lang="ts">
	import type { JobView } from '$lib/types/api.gen';
	import { pauseWaitingJob, retryFailedTitles, startWaitingJob } from '$lib/api/jobs';
	import { previouslyRippedJobId } from '$lib/utils/job-status';
	import { isAdmin } from '$lib/stores/auth';
	import ReviewWaitStatus from './ReviewWaitStatus.svelte';
	import JobTrackPicker from './JobTrackPicker.svelte';

	// The job page's review-gate panel for a held disc: how it will go on
	// (confirm or countdown, via ReviewWaitStatus) and the Start action, so a
	// held disc can be released from its own page as well as from the
	// dashboard card. Two stops (fork only) for a disc backed up whole first:
	// stop 1 (awaiting_review) starts the backup, stop 2 (awaiting_titles)
	// rips the titles ticked in the Tracks table from it (retry-failed).
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
		/** Stop 1 of a disc backed up whole first (`JobDetailView.parks_after_backup`). */
		parksAfterBackup?: boolean;
		/** The job changed (started, or its countdown paused): reload it. */
		onchanged?: () => void;
	}

	let {
		job,
		requiresConfirm,
		waitSeconds,
		paused = false,
		keptCount = null,
		parksAfterBackup = false,
		onchanged
	}: Props = $props();

	let busy = $state(false);
	let error = $state<string | null>(null);

	// A previously-ripped hold waits for Rip again whatever the mode.
	const previousRip = $derived(job.status === 'awaiting_review' && previouslyRippedJobId(job) !== null);
	const awaitingConfirm = $derived(requiresConfirm && !previousRip);
	// Stop 2 opens (its countdown starts) once the menu has named the titles.
	const stop2 = $derived(job.status === 'awaiting_titles');
	const opened = $derived(!stop2 || job.wait_start_time != null);
	const backupFirst = $derived(parksAfterBackup && !stop2 && !previousRip);
	const confirmMessage = $derived(
		stop2
			? "These titles won't be ripped until you confirm them. Tick the ones to keep in the Tracks table."
			: backupFirst
				? "This disc won't be backed up until you confirm it. Check its identity first: its titles are chosen after the backup."
				: "This disc won't start ripping until you confirm it. Check the title and tracks first."
	);
	const goMessage = $derived(
		stop2
			? 'The ticked titles are ripped from the disc backup when the countdown ends.'
			: backupFirst
				? 'The disc is backed up when the countdown ends. Its titles are chosen after.'
				: 'The rip starts by itself when the countdown ends.'
	);
	const message = $derived(
		previousRip
			? 'ARM ripped this disc before. Rip it again, or abandon this job.'
			: !opened
				? 'The disc is backed up. Reading the disc menu: its names go on the title rows, then the titles can be chosen.'
				: requiresConfirm
					? confirmMessage
					: paused
						? 'Ripping is paused, so the countdown is on hold.'
						: job.manual_pause
							? 'The countdown is paused for this disc.'
							: goMessage
	);
	const nothingKept = $derived(keptCount === 0 && !previousRip && !backupFirst);
	const startLabel = $derived(
		previousRip
			? 'Rip again'
			: stop2
				? 'Rip titles'
				: backupFirst
					? 'Begin ripping disc to backup/menu'
					: awaitingConfirm
						? 'Confirm & start rip'
						: 'Start rip now'
	);

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

	// DVD and Blu-ray: the audio/subtitle choice, saved when the rip starts.
	const isVideoDisc = $derived(job.disc_type === 'dvd' || job.disc_type === 'bluray');
	let picker = $state<JobTrackPicker | null>(null);

	const start = () =>
		run(async () => {
			if (stop2) return retryFailedTitles(job.id);
			await picker?.save();
			return startWaitingJob(job.id);
		}, 'Failed to start the rip');
	const setPause = (p: boolean) => run(() => pauseWaitingJob(job.id, p), 'Failed to update the pause');
</script>

<section class="review-gate-panel stack" data-testid="review-gate-panel">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<h2 class="review-gate-panel-title">
			{previousRip ? 'Already ripped' : stop2 ? 'Choose the titles' : 'Held for review'}
		</h2>
		{#if !previousRip && opened}
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
	{#if isVideoDisc && !previousRip && !stop2}
		<details class="review-gate-panel-tracks" data-testid="review-gate-tracks" open>
			<summary class="field-label">Audio and subtitle tracks</summary>
			<JobTrackPicker bind:this={picker} jobId={job.id} variant="review" />
		</details>
	{/if}
	{#if error}
		<p class="review-gate-panel-error" role="alert">{error}</p>
	{/if}
	{#if $isAdmin && opened}
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
	.review-gate-panel-tracks summary {
		cursor: pointer;
		margin-bottom: 0.5rem;
	}
	.review-gate-panel-error {
		font-size: 0.875rem;
		color: var(--color-danger);
	}
</style>
