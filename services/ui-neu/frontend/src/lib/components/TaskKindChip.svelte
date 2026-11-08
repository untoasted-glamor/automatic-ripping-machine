<script lang="ts">
	import type { TranscodeTaskKind } from '$lib/types/api.gen';
	import { SUBTITLE_OCR_LABEL } from '$lib/utils/transcode-kind';

	// Labels a transcode task that is not an encode. An encode shows nothing.
	interface Props {
		kind?: TranscodeTaskKind | null;
	}

	let { kind }: Props = $props();
</script>

{#if kind === 'subtitle_ocr'}
	<span
		class="chip chip-info chip-sm task-kind-chip"
		title="Text subtitles (.srt) read from the image subtitles of a finished encode"
		data-testid="task-kind-chip">{SUBTITLE_OCR_LABEL}</span
	>
{/if}

<style>
	.task-kind-chip {
		white-space: nowrap;
	}
</style>
