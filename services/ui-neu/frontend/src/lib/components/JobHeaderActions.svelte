<script lang="ts">
	import JobActions from '$lib/components/JobActions.svelte';
	import MakeTextSubtitlesButton from '$lib/components/MakeTextSubtitlesButton.svelte';
	import { isAdmin } from '$lib/stores/auth';
	import type { JobDetail } from '$lib/stores/jobDetail.svelte';
	import type { JobSubtitleOcrView, JobView, SubtitleOcrResponse } from '$lib/types/api.gen';
	import { isAwaitingIdentity } from '$lib/utils/job-status-groups';

	interface Props {
		job: JobView;
		jobDetail: JobDetail;
		// A video or CD job has the Identity panel: the button opens it (the media
		// type switch is on the panel), else it opens the Identify dialog.
		hasIdentityPanel: boolean;
		// The job's text subtitles (.srt): offered while an encode has none.
		subtitleOcr: JobSubtitleOcrView | null | undefined;
		ontextsubtitles: (resp: SubtitleOcrResponse) => void;
		onidentify: () => void;
		onapply: () => void;
		ondelete: () => void;
	}

	let { job, jobDetail, hasIdentityPanel, subtitleOcr, ontextsubtitles, onidentify, onapply, ondelete }: Props =
		$props();

	// Identify is "Edit identity" once an identity already exists (post-rip /
	// identified), otherwise "Identify disc" for the auto-id-failed case.
	let identifyLabel = $derived(
		hasIdentityPanel ? 'Identity' : isAwaitingIdentity(job.status) ? 'Identify disc' : 'Edit identity'
	);
</script>

<div class="flex flex-wrap items-center gap-2 ml-auto">
	{#if (hasIdentityPanel || jobDetail.canResolve) && $isAdmin}
		<button type="button" data-testid="identify-open" onclick={onidentify} class="btn job-detail-header-btn">
			{identifyLabel}
		</button>
	{/if}
	{#if jobDetail.canApply && $isAdmin}
		<button type="button" data-testid="apply-open" onclick={onapply} class="btn job-detail-header-btn">
			Apply session
		</button>
	{/if}
	{#if subtitleOcr && subtitleOcr.eligible_encodes > 0 && $isAdmin}
		<MakeTextSubtitlesButton jobId={job.id} view={subtitleOcr} ondone={ontextsubtitles} />
	{/if}
	{#if $isAdmin}
		<JobActions {job} onaction={jobDetail.load} {ondelete} />
	{/if}
</div>

<style>
	/* the original header buttons were px-3 py-1.5 text-sm (0.75rem/0.375rem,
	   0.875rem) - between .btn's default and .btn-sm */
	.job-detail-header-btn {
		min-height: auto;
		padding: 0.375rem 0.75rem;
		font-size: 0.875rem;
		line-height: 1.25rem;
	}
</style>
