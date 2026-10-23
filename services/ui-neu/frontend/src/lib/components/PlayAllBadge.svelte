<script lang="ts">
	import type { TrackView } from '$lib/types/api.gen';

	// Next to a play-all's Keep box: why it starts left out. The review gate's
	// detector sets `role=play_all` with `play_all_detector` provenance; an
	// identity source or the operator may set it too.
	interface Props {
		track: TrackView;
	}

	let { track }: Props = $props();

	let detected = $derived(track.identity_provenance?.role === 'play_all_detector');
	const TIP =
		'It plays other titles on this disc back to back, so ripping it duplicates them. It starts left out; tick Keep to rip it anyway.';
</script>

{#if track.role === 'play_all'}
	<span class="badge badge-sm badge-info play-all-badge" title={TIP} data-testid="play-all-badge">
		{detected ? 'likely play-all' : 'play-all'}
	</span>
{/if}

<style>
	.play-all-badge {
		margin-left: 0.375rem;
		white-space: nowrap;
		cursor: help;
	}
</style>
