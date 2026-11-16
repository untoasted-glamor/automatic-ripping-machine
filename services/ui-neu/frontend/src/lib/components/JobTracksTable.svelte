<script lang="ts">
	import type { Snippet } from 'svelte';
	import JobTrackRow from '$lib/components/JobTrackRow.svelte';
	import { isPostRipStatus, isRipperBusyStatus } from '$lib/utils/job-status-groups';
	import { keepLockedReason } from '$lib/utils/track-keep';
	import type { JobView, MenuProposalView, NamingPreviewItem, TrackView } from '$lib/types/api.gen';

	interface Props {
		job: JobView;
		tracks: TrackView[];
		tracksAreSeries: boolean;
		previewByTrack: ReadonlyMap<string, NamingPreviewItem>;
		ontitleapplied: () => void;
		// Keep: the job parks after its backup, titles are being chosen (stop 2), and how many titles are kept.
		parks: boolean;
		choosing: boolean;
		keptCount: number;
		onkeepchanged: () => void;
		onkeeperror: (message: string | null) => void;
		// The disc menu: the job has a copied menu, each title's menu name, whose menu-name row is open, and the row a
		// menu box last jumped to.
		hasMenu: boolean;
		menuByTrack: ReadonlyMap<string, MenuProposalView>;
		menuRowOpen: (trackId: string) => boolean;
		ontogglemenurow: (trackId: string) => void;
		onmenuchange: (updated: MenuProposalView) => void;
		onmenunamed: (created: MenuProposalView) => void;
		flashTrackId: string | null;
		// Shown above the table (a refused Keep change) and under it (the Keep summary and its actions).
		notice?: Snippet;
		footer?: Snippet;
	}

	let {
		job,
		tracks,
		tracksAreSeries,
		previewByTrack,
		ontitleapplied,
		parks,
		choosing,
		keptCount,
		onkeepchanged,
		onkeeperror,
		hasMenu,
		menuByTrack,
		menuRowOpen,
		ontogglemenurow,
		onmenuchange,
		onmenunamed,
		flashTrackId,
		notice,
		footer
	}: Props = $props();

	// The Episode column: a series' episodes, and on a disc with a menu each title's menu name (click to name it).
	let nameColumn = $derived(tracksAreSeries || hasMenu);
	let ripStarted = $derived(isRipperBusyStatus(job.status) || isPostRipStatus(job.status));

	let editingTrackId = $state<string | null>(null);

	function handleTrackTitleApply() {
		editingTrackId = null;
		ontitleapplied();
	}
</script>

<section>
	<div class="mb-3 flex items-center justify-between">
		<h2 class="job-detail-section-title">
			Tracks ({tracks.length})
		</h2>
	</div>
	{@render notice?.()}
	<div class="overflow-x-auto job-detail-table-scroll">
		<table class="table responsive-table">
			<thead>
				<tr>
					<th class="table-header">#</th>
					<th class="table-header">Kind</th>
					<th class="table-header">Title</th>
					{#if nameColumn}
						<th class="table-header">{tracksAreSeries ? 'Episode' : 'Extra'}</th>
					{/if}
					<th class="table-header">Filename</th>
					<th class="table-header">Length</th>
					<th class="table-header">Size</th>
					<th class="table-header">Keep</th>
					<th class="table-header">Rip</th>
					<th class="table-header">Transcode</th>
				</tr>
			</thead>
			<tbody>
				{#each tracks as track (track.id)}
					<JobTrackRow
						{job}
						{track}
						preview={previewByTrack.get(track.id)}
						{tracksAreSeries}
						editing={editingTrackId === track.id}
						ontoggleedit={() => {
							editingTrackId = editingTrackId === track.id ? null : track.id;
						}}
						oncloseedit={() => {
							editingTrackId = null;
						}}
						ontitleapplied={handleTrackTitleApply}
						{ripStarted}
						lockedReason={keepLockedReason(job.status, parks)}
						lastKept={choosing && keptCount === 1}
						{onkeepchanged}
						{onkeeperror}
						{hasMenu}
						named={menuByTrack.get(track.id)}
						menuRowOpen={menuRowOpen(track.id)}
						ontogglemenurow={() => ontogglemenurow(track.id)}
						{onmenuchange}
						{onmenunamed}
						flash={flashTrackId === track.id}
					/>
				{/each}
			</tbody>
		</table>
	</div>
	{@render footer?.()}
</section>

<style>
	.job-detail-section-title {
		font-size: 1.125rem;
		line-height: 1.75rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.job-detail-table-scroll {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
	}
</style>
