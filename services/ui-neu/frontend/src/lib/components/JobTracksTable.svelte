<script lang="ts">
	import type { Snippet } from 'svelte';
	import { posterSrc, posterFallback } from '$lib/utils/poster';
	import StatusBadge from '$lib/components/StatusBadge.svelte';
	import TaskKindChip from '$lib/components/TaskKindChip.svelte';
	import TrackTitleSearch from '$lib/components/TrackTitleSearch.svelte';
	import TrackKeepToggle from '$lib/components/TrackKeepToggle.svelte';
	import PlayAllBadge from '$lib/components/PlayAllBadge.svelte';
	import MenuProposalEditor from '$lib/components/MenuProposalEditor.svelte';
	import MenuNameByHand from '$lib/components/MenuNameByHand.svelte';
	import { formatFps } from '$lib/utils/fps';
	import { trackKindLabel, trackSizeLabel } from '$lib/utils/track-fields';
	import { trackDuration } from '$lib/utils/duration';
	import { isPostRipStatus, isRipperBusyStatus } from '$lib/utils/job-status-groups';
	import { isDiscMenu, keepLockedReason, menuSkippedReason, trackRipStatus } from '$lib/utils/track-keep';
	import { isAdmin } from '$lib/stores/auth';
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
					{@const preview = previewByTrack.get(track.id)}
					{@const length = trackDuration(track.duration_seconds, track.expected_duration_seconds)}
					{@const nameable = $isAdmin && hasMenu && track.kind === 'video_title'}
					<tr
						class="table-row job-detail-track-row"
						class:job-detail-row-flash={flashTrackId === track.id}
						id="track-row-{track.id}"
						data-disabled={track.excluded}
					>
						<td class="table-cell" data-label="#">{isDiscMenu(track) ? '-' : track.index}</td>
						<td class="table-cell job-detail-track-kind" data-label="Kind">{trackKindLabel(track.kind)}</td>
						<td
							class="table-cell {$isAdmin && !isDiscMenu(track) ? 'job-detail-track-title-cell' : ''}"
							data-label="Title"
							onclick={$isAdmin && !isDiscMenu(track)
								? () => {
										editingTrackId = editingTrackId === track.id ? null : track.id;
									}
								: undefined}
						>
							{#if isDiscMenu(track)}
								<span class="job-detail-track-title job-detail-track-title-strong" data-testid="menu-row"
									>Disc menu</span
								>
								{#if menuSkippedReason(track)}
									<span class="job-detail-track-faint" data-testid="menu-skip-reason">({menuSkippedReason(track)})</span
									>
								{/if}
							{:else if track.title}
								<div class="flex items-center gap-1.5">
									{#if track.poster_url}
										<img
											data-poster
											src={posterSrc(track.poster_url)}
											alt=""
											class="job-detail-track-poster"
											onerror={posterFallback}
										/>
									{/if}
									<div>
										<span class="job-detail-track-title job-detail-track-title-strong">{track.title}</span>
										{#if track.year}
											<span class="job-detail-track-year"> ({track.year})</span>
										{/if}
										<div class="mt-0.5 flex flex-wrap items-center gap-1">
											{#if track.imdb_id}
												<a
													href="https://www.imdb.com/title/{track.imdb_id}"
													target="_blank"
													rel="noopener noreferrer"
													onclick={(e) => e.stopPropagation()}
													class="badge badge-imdb job-detail-track-imdb-badge">IMDb</a
												>
											{/if}
											{#if track.edition}
												<span class="job-detail-track-meta-pill">{track.edition}</span>
											{/if}
											{#if track.role}
												<span class="job-detail-track-meta-pill">{track.role}</span>
											{/if}
										</div>
									</div>
								</div>
							{:else}
								<span class="job-detail-track-untitled"
									>{job.title || 'Untitled'}{#if job.year}
										({job.year}){/if}</span
								>
							{/if}
						</td>
						{#if nameColumn}
							<td
								class="table-cell {nameable ? 'job-detail-track-title-cell' : ''}"
								data-label={tracksAreSeries ? 'Episode' : 'Extra'}
								data-testid="track-name-cell"
								title={nameable ? (menuRowOpen(track.id) ? 'Hide its menu name' : 'Name it from the menu') : undefined}
								onclick={nameable ? () => ontogglemenurow(track.id) : undefined}
							>
								{#if track.episode_number != null}
									<span class="job-detail-track-title job-detail-track-title-strong">{track.episode_number}</span>
									{#if track.episode_name}
										<span class="ml-1.5 job-detail-track-episode-name">{track.episode_name}</span>
									{/if}
								{:else}
									<span class="job-detail-track-faint">-</span>
								{/if}
							</td>
						{/if}
						<td
							class="truncate table-cell mono job-detail-track-filename"
							data-label="Filename"
							title={preview?.output_name ?? ''}
						>
							{#if preview?.output_name}
								<span>{preview.output_name}</span>
								{#if track.custom_filename}
									<span class="ml-1 badge badge-sm badge-warning">custom</span>
								{/if}
							{:else}
								<span class="job-detail-track-faint">{track.excluded ? 'excluded' : '-'}</span>
							{/if}
						</td>
						<td class="table-cell" data-label="Length" data-testid="track-duration">
							{#if length && !length.estimated}
								{length.label}
							{:else if length}
								<span class="job-detail-track-faint" title="Scan-time estimate">~{length.label}</span>
							{:else}-{/if}
						</td>
						<td class="table-cell job-detail-track-kind" data-label="Size">{trackSizeLabel(track)}</td>
						<td class="table-cell" data-label="Keep">
							{#if $isAdmin}
								<TrackKeepToggle
									jobId={job.id}
									{track}
									lockedReason={keepLockedReason(job.status, parks)}
									lastKept={choosing && keptCount === 1}
									onchanged={onkeepchanged}
									onerror={onkeeperror}
								/>
							{:else}
								<span class="job-detail-track-untitled">{track.excluded ? 'Left out' : 'Kept'}</span>
							{/if}
							<PlayAllBadge {track} />
						</td>
						<!-- Rip outcome -->
						<td class="table-cell" data-label="Rip">
							<span class="flex items-center gap-1">
								<StatusBadge status={trackRipStatus(track, ripStarted)} />
								{#if track.attempts > 1}
									<span class="job-detail-track-attempts" title="Rip attempts">x{track.attempts}</span>
								{/if}
							</span>
						</td>
						<!-- Transcode outcome (— when no transcode task yet) -->
						<td class="table-cell" data-label="Transcode">
							{#if track.transcode_status}
								<span class="flex items-center gap-1">
									<StatusBadge status={track.transcode_status} />
									{#if track.transcode_status === 'in_progress' && track.transcode_fps != null}
										<span class="job-detail-track-faint" title="Encoder frame rate"
											>{formatFps(track.transcode_fps, track.transcode_avg_fps)}</span
										>
									{/if}
								</span>
							{:else}
								<span class="job-detail-track-faint">-</span>
							{/if}
							{#if track.subtitle_ocr_status}
								<span class="job-detail-track-ocr" data-testid="track-ocr-status">
									<TaskKindChip kind="subtitle_ocr" />
									<StatusBadge status={track.subtitle_ocr_status} />
								</span>
							{/if}
						</td>
					</tr>
					{#if track.status === 'failed' && track.last_error}
						<tr>
							<td colspan="99" class="table-cell job-detail-track-error" data-label="">
								<span class="job-detail-error-strong">Error:</span>
								{track.last_error}
							</td>
						</tr>
					{/if}
					{@const named = menuByTrack.get(track.id)}
					{#if menuRowOpen(track.id) && (named || nameable)}
						<tr data-testid="track-menu-name" class:job-detail-row-flash={flashTrackId === track.id}>
							<td colspan="99" class="table-cell" data-label="Menu name">
								{#if named}
									<MenuProposalEditor
										jobId={job.id}
										proposal={named}
										trackStatus={track.status}
										canEdit={$isAdmin}
										onchange={onmenuchange}
									/>
								{:else}
									<MenuNameByHand
										jobId={job.id}
										trackId={track.id}
										onsaved={onmenunamed}
										oncancel={() => ontogglemenurow(track.id)}
									/>
								{/if}
							</td>
						</tr>
					{/if}
					{#if editingTrackId === track.id}
						<tr>
							<td colspan="99" class="table-cell" data-label="">
								<TrackTitleSearch
									jobId={job.id}
									{track}
									mediaType={job.media_type}
									onapply={handleTrackTitleApply}
									onclose={() => {
										editingTrackId = null;
									}}
								/>
							</td>
						</tr>
					{/if}
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
	.job-detail-track-kind {
		color: var(--color-text-secondary);
	}
	.job-detail-track-title-cell {
		cursor: pointer;
	}
	.job-detail-track-title-cell:hover {
		background: var(--color-primary-tint-1);
	}
	.job-detail-track-poster {
		height: 2rem;
		width: 1.25rem;
		border-radius: var(--radius-sm);
		object-fit: cover;
	}
	.job-detail-track-title {
		color: var(--color-text);
	}
	.job-detail-track-title-strong {
		font-weight: 500;
	}
	.job-detail-error-strong {
		font-weight: 600;
	}
	.job-detail-track-year {
		color: var(--color-text-faint);
	}
	.job-detail-track-imdb-badge {
		border-radius: 9999px;
		padding: 0.125rem 0.375rem;
		font-size: 9px;
	}
	.job-detail-track-meta-pill {
		border-radius: var(--radius-sm);
		padding: 0.125rem 0.375rem;
		font-size: 9px;
		font-weight: 500;
		background: var(--color-primary-tint-2);
		color: var(--color-text-secondary);
	}
	.job-detail-track-untitled {
		font-size: 0.75rem;
		line-height: 1rem;
		color: var(--color-text-faint);
	}
	.job-detail-track-episode-name {
		font-size: 0.75rem;
		line-height: 1rem;
		color: var(--color-text-muted);
	}
	.job-detail-track-faint {
		color: var(--color-text-faint);
	}
	.job-detail-track-filename {
		max-width: 260px;
		color: var(--color-text-secondary);
	}
	.job-detail-track-attempts {
		font-size: 10px;
		color: var(--color-text-faint);
	}
	.job-detail-track-error {
		background: var(--color-danger-soft);
		color: var(--color-on-danger-soft);
	}
	.job-detail-track-row {
		/* A menu box's jump lands the row clear of the top bar. */
		scroll-margin-top: 5rem;
	}
	.job-detail-row-flash > :global(td) {
		background: color-mix(in srgb, var(--color-primary) 18%, transparent);
		transition: background var(--motion-fast) var(--ease);
	}
	.job-detail-track-ocr {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.25rem;
		margin-top: 0.25rem;
	}
</style>
