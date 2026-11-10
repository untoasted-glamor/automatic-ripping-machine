<script lang="ts">
	// The model the transcode image reads subtitle images with. Built in at
	// image build time and checked against this SHA-256; ARM never downloads
	// one at run time.
	import type { SubtitleOcrModelView } from '$lib/types/api.gen';
	import { formatBytes } from '$lib/utils/format';

	let { model }: { model: SubtitleOcrModelView | null | undefined } = $props();
</script>

{#if model}
	<dl class="text-subtitles-model" data-testid="text-subtitles-model" aria-label="Text subtitles model">
		<dt>Model</dt>
		<dd>{model.name} ({model.version}), {formatBytes(model.size)}</dd>
		<dt>SHA-256</dt>
		<dd class="text-subtitles-model-hash">{model.sha256}</dd>
		<dt>Licence</dt>
		<dd>{model.licence}</dd>
		<dt>Source</dt>
		<dd class="text-subtitles-model-hash">{model.source}</dd>
	</dl>
	<p class="field-help text-subtitles-model-note">Built into the transcode image. ARM never downloads a model.</p>
{/if}

<style>
	.text-subtitles-model {
		display: grid;
		grid-template-columns: max-content minmax(0, 1fr);
		gap: 0.25rem 0.75rem;
		margin: 0 0 0.5rem;
		font-size: 0.8125rem;
	}
	.text-subtitles-model dt {
		color: var(--color-text-muted);
	}
	.text-subtitles-model dd {
		margin: 0;
	}
	.text-subtitles-model-hash {
		font-family: var(--font-mono);
		overflow-wrap: anywhere;
	}
	.text-subtitles-model-note {
		margin-bottom: 1rem;
	}
</style>
