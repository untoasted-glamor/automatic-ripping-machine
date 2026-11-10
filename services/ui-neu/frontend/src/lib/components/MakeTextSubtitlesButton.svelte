<script lang="ts">
	// Job page: make text subtitles (.srt) for the job's finished encodes
	// after the fact (POST /api/jobs/{id}/subtitle-ocr). Shown while text
	// subtitles are on in Settings and an encode wrote subtitle images. When
	// some encodes already have .srt files, it asks whether to replace them
	// or make only the missing ones.
	import { requestSubtitleOcr } from '$lib/api/jobs';
	import type { JobSubtitleOcrView, SubtitleOcrResponse } from '$lib/types/api.gen';

	interface Props {
		jobId: string;
		view: JobSubtitleOcrView;
		/** Called with the result once tasks are queued (or none were due). */
		ondone?: (resp: SubtitleOcrResponse) => void;
	}

	let { jobId, view, ondone }: Props = $props();

	let asking = $state(false);
	let working = $state(false);
	let error = $state<string | null>(null);

	const eligible = $derived(view.eligible_encodes ?? 0);
	const existing = $derived(view.with_text_subtitles ?? 0);
	const missing = $derived(eligible - existing);

	async function run(overwrite: boolean): Promise<void> {
		asking = false;
		working = true;
		error = null;
		try {
			ondone?.(await requestSubtitleOcr(jobId, overwrite));
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not queue the text subtitles';
		} finally {
			working = false;
		}
	}

	function clicked(): void {
		if (existing === 0) void run(false);
		else asking = true;
	}
</script>

<div class="cluster make-text-subtitles">
	{#if asking}
		<span class="field-help" data-testid="mts-question">
			{existing} of {eligible} already {existing === 1 ? 'has' : 'have'}
			text subtitles.
		</span>
		{#if missing > 0}
			<button type="button" class="btn btn-sm" data-testid="mts-missing" onclick={() => run(false)}>
				Only the missing {missing}
			</button>
		{/if}
		<button type="button" class="btn btn-sm" data-testid="mts-replace" onclick={() => run(true)}>Replace all</button>
		<button type="button" class="btn btn-sm" data-testid="mts-cancel" onclick={() => (asking = false)}>Cancel</button>
	{:else}
		<button
			type="button"
			class="btn mts-open"
			data-testid="mts-open"
			disabled={working}
			title="Read text subtitles (.srt) from the subtitle images of this job's finished encodes"
			onclick={clicked}
		>
			{working ? 'Queueing...' : 'Make text subtitles'}
		</button>
	{/if}
	{#if error}
		<span class="field-error" data-testid="mts-error">{error}</span>
	{/if}
</div>

<style>
	.make-text-subtitles {
		gap: 0.375rem;
		align-items: center;
	}
	/* Sized like the job page's other header buttons (Apply session). */
	.mts-open {
		min-height: auto;
		padding: 0.375rem 0.75rem;
		font-size: 0.875rem;
		line-height: 1.25rem;
	}
</style>
