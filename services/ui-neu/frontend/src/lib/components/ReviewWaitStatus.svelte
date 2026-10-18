<script lang="ts">
	import type { JobView } from '$lib/types/api.gen';
	import CountdownTimer from './CountdownTimer.svelte';
	import Glyph from './Glyph.svelte';

	// How a disc held at the review gate (awaiting_review) will get to the rip,
	// shared by the dashboard review card and the job page. Mandatory-confirm
	// mode (config review_requires_confirm) has no countdown: the disc waits for
	// Start. Timed mode shows the cosmetic countdown (the ripper owns the real
	// clock), frozen by the global pause or this disc's own pause; its
	// pause/resume toggles this disc only. A previously-ripped hold has no
	// countdown either; callers word that case themselves.
	interface Props {
		job: Pick<JobView, 'wait_start_time' | 'manual_pause'>;
		requiresConfirm: boolean;
		waitSeconds: number;
		/** Global ripping pause: freezes every countdown. */
		paused?: boolean;
		/** Render for a coloured background (the review card's status bar). */
		inverted?: boolean;
		onpause?: () => void;
		onresume?: () => void;
	}

	let { job, requiresConfirm, waitSeconds, paused = false, inverted = false, onpause, onresume }: Props = $props();
</script>

{#if requiresConfirm}
	<span class="review-wait-status" data-inverted={inverted} data-testid="review-wait-confirm">
		<Glyph name="clock" class="h-3.5 w-3.5" />
		Waiting for your confirmation
	</span>
{:else if job.wait_start_time}
	<CountdownTimer
		startTime={job.wait_start_time}
		{waitSeconds}
		paused={paused || !!job.manual_pause}
		{inverted}
		{onpause}
		{onresume}
	/>
{/if}

<style>
	.review-wait-status {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--color-primary-text);
	}
	/* inverted: same on-primary tone CountdownTimer uses on the coloured bar. */
	.review-wait-status[data-inverted='true'] {
		color: color-mix(in srgb, var(--color-on-primary) 90%, transparent);
	}
</style>
