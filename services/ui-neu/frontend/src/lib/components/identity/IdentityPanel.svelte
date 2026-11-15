<script lang="ts">
	// The job page's one place to identify a disc and name its titles (fork plan v3, C1).
	// It is assembled from the components the page used before and makes the same calls:
	//   1. Search and pick: TitleSearch (resolve, or the poster alone when the job can't
	//      be resolved), with the Movie / TV switch in the panel's bar.
	//   2. Season and disc: IdentityFields, saved through resolve with the title as it is.
	//   3. Episodes (TV): EpisodeMatchPanel.
	// A CD gets MusicSearch instead. IdentifyDialog stays for typing an identity in by hand.
	import type { JobView, TrackView } from '$lib/types/api.gen';
	import { resolveJob } from '$lib/api/jobs';
	import { isAdmin } from '$lib/stores/auth';
	import { isAwaitingIdentity } from '$lib/utils/job-status-groups';
	import { changedPosition, identityOpensFor, positionOf } from '$lib/utils/identity-position';
	import TitleSearch from '$lib/components/TitleSearch.svelte';
	import MusicSearch from '$lib/components/MusicSearch.svelte';
	import EpisodeMatchPanel from '$lib/components/episodes/EpisodeMatchPanel.svelte';
	import MediaTypeSwitch from '$lib/components/episodes/MediaTypeSwitch.svelte';
	import IdentityFields from './IdentityFields.svelte';

	interface Props {
		job: JobView;
		tracks: TrackView[];
		// The Backend is re-matching episodes (after a type flip or a series pick).
		matching?: boolean;
		// Tracks whose episode or role was set by hand (switching to Movie asks first).
		handSetCount: number;
		open?: boolean;
		// The identity changed (a pick, a poster, the season or disc, a CD match): reload the job.
		onchanged: () => void;
		// A series was picked: episode matching starts.
		onseries: () => void;
		ontypechanged: (type: 'movie' | 'tv') => void;
		// Type the identity in by hand (IdentifyDialog).
		onmanual: () => void;
	}

	let {
		job,
		tracks,
		matching = false,
		handSetCount,
		open = $bindable(false),
		onchanged,
		onseries,
		ontypechanged,
		onmanual
	}: Props = $props();

	let isCd = $derived(job.disc_type === 'cd');
	let isTv = $derived(job.media_type === 'tv');
	let canResolve = $derived(job.actions.can_resolve);
	let awaitingIdentity = $derived(isAwaitingIdentity(job.status));

	// Opens by itself when the job comes to wait for the operator (and on a first load there), until the
	// operator accepts an identity on this page: an accept collapses it (`accepted`), and the next stop
	// the accept moves the job to doesn't open it again.
	let seenStatus: string | null = null;
	let acceptedFor: string | null = null;
	$effect(() => {
		const status = job.status;
		if (status === seenStatus) return;
		seenStatus = status;
		if (identityOpensFor(status) && acceptedFor !== job.id) open = true;
	});

	/** An identity was accepted (a pick, an episode placement, or the dialog): collapse the panel. */
	export function accepted(): void {
		acceptedFor = job.id;
		open = false;
	}

	function picked(): void {
		accepted();
		onchanged();
	}

	// The search remounts with a type when the episode step asks for a series search.
	let searchType = $state<'movie' | 'tv' | undefined>(undefined);
	let searchKey = $state(0);
	let episodePanel = $state<{ reload: () => Promise<void> } | undefined>(undefined);

	/** Reload the episode step (the page calls it on the job's identity events). */
	export async function reloadEpisodes(): Promise<void> {
		await episodePanel?.reload();
	}

	/** Open the panel at its search step, optionally searching for a series. */
	export function showSearch(type?: 'movie' | 'tv'): void {
		open = true;
		searchType = type;
		searchKey += 1;
		requestAnimationFrame(() =>
			document.getElementById('identity-step-search')?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
		);
	}

	// Step 2. Only a job that already has an identity: one that waits for it is identified by a pick or by hand.
	let positionEditable = $derived(!isCd && canResolve && !awaitingIdentity && !!job.title?.trim() && $isAdmin);
	let season = $state<number | null>(null);
	let discNumber = $state<number | null>(null);
	let discTotal = $state<number | null>(null);
	let savedKey = '';
	$effect(() => {
		// Follow the job's saved values when they change (a reload), not while the operator types.
		const p = positionOf(job);
		const key = `${p.season}/${p.discNumber}/${p.discTotal}`;
		if (key === savedKey) return;
		savedKey = key;
		season = p.season;
		discNumber = p.discNumber;
		discTotal = p.discTotal;
	});
	let change = $derived(changedPosition(job, { season, discNumber, discTotal }));
	let positionDirty = $derived(Object.keys(change).length > 0);
	let savingPosition = $state(false);
	let positionError = $state<string | null>(null);

	async function savePosition(): Promise<void> {
		savingPosition = true;
		positionError = null;
		try {
			await resolveJob(job.id, { title: (job.title ?? '').trim(), year: job.year ?? null, ...change });
			onchanged();
		} catch (e) {
			positionError = e instanceof Error ? e.message : 'Could not save the season and disc';
		} finally {
			savingPosition = false;
		}
	}
</script>

<section class="identity-panel" id="identity-panel" data-testid="identity-panel">
	<div class="identity-panel-bar">
		<button
			type="button"
			class="identity-panel-toggle"
			aria-expanded={open}
			aria-controls="identity-panel-body"
			data-testid="identity-toggle"
			onclick={() => (open = !open)}
		>
			Identity
		</button>
		<div class="identity-panel-bar-actions">
			{#if !isCd && $isAdmin}
				<MediaTypeSwitch {job} {handSetCount} onchanged={ontypechanged} />
			{/if}
			{#if canResolve && $isAdmin}
				<button type="button" class="btn btn-sm" data-testid="identify-manual" onclick={onmanual}>
					{awaitingIdentity ? 'Identify by hand' : 'Edit by hand'}
				</button>
			{/if}
		</div>
	</div>

	{#if open}
		<div id="identity-panel-body" class="identity-panel-body">
			{#if isCd}
				<section class="identity-panel-step" data-testid="identity-step-music">
					<MusicSearch {job} discTracks={tracks} onapply={picked} />
				</section>
			{:else}
				<section class="identity-panel-step" id="identity-step-search" data-testid="identity-step-search">
					<h3 class="identity-panel-step-title">Search and pick</h3>
					{#key searchKey}
						<TitleSearch {job} onapply={picked} {onseries} initialType={searchType} />
					{/key}
				</section>
				{#if positionEditable}
					<section class="identity-panel-step" data-testid="identity-step-position">
						<h3 class="identity-panel-step-title">{isTv ? 'Season and disc' : 'Disc'}</h3>
						<IdentityFields bind:season bind:discNumber bind:discTotal disabled={savingPosition} />
						{#if positionError}
							<p class="field-error" role="alert">{positionError}</p>
						{/if}
						<button
							type="button"
							class="btn btn-sm"
							data-testid="identity-position-save"
							disabled={!positionDirty || savingPosition}
							onclick={savePosition}
						>
							{savingPosition ? 'Saving...' : 'Save'}
						</button>
					</section>
				{/if}
				{#if isTv}
					<section class="identity-panel-step" data-testid="identity-step-episodes">
						<h3 class="identity-panel-step-title">Episodes</h3>
						<EpisodeMatchPanel
							bind:this={episodePanel}
							{job}
							{tracks}
							{matching}
							onsearchseries={() => showSearch('tv')}
							onapplied={accepted}
						/>
					</section>
				{/if}
			{/if}
		</div>
	{/if}
</section>

<style>
	.identity-panel {
		border-top: 1px solid var(--color-border);
	}
	.identity-panel-bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 1rem;
		background: color-mix(in srgb, var(--color-surface) 50%, transparent);
	}
	.identity-panel-toggle {
		font-size: 0.875rem;
		line-height: 1.25rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.identity-panel-toggle::before {
		content: '+ ';
		color: var(--color-text-muted);
	}
	.identity-panel-toggle[aria-expanded='true']::before {
		content: '- ';
	}
	.identity-panel-bar-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		margin-left: auto;
	}
	.identity-panel-body {
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
		border-top: 1px solid var(--color-border);
		padding: 1.25rem;
	}
	.identity-panel-step + .identity-panel-step {
		border-top: 1px solid var(--color-border);
		padding-top: 1.25rem;
	}
	.identity-panel-step-title {
		margin-bottom: 0.75rem;
		font-size: 0.8125rem;
		font-weight: 600;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		color: var(--color-text-muted);
	}
</style>
