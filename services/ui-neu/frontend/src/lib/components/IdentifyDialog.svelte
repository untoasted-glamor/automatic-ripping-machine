<script lang="ts">
	import { resolveJob, searchMetadata, updateJobTitle } from '$lib/api/jobs';
	import type { JobView, MetadataCandidate, ResolveResponse } from '$lib/types/api.gen';
	import PosterImage from './PosterImage.svelte';
	import { driveLabel } from '$lib/utils/drive-name';
	import { isAwaitingIdentity } from '$lib/utils/job-status-groups';

	let {
		job,
		driveNames,
		onclose,
		onidentified
	}: {
		job: JobView;
		driveNames?: Record<string, string> | null;
		onclose: () => void;
		onidentified: (resp: ResolveResponse) => void;
	} = $props();

	// `isCd` switches the form body between the two-field (title + year)
	// video shape and the structured-music shape. The music path template
	// requires {artist}, {album}, and per-track {track_title} — none of
	// which a title-only resolve can populate.
	const isCd = $derived(job.disc_type === 'cd');

	// `isEditMode` distinguishes the "auto-identify failed, please fill in"
	// case (status awaiting_user_id / ripped_awaiting_identify) from the
	// "auto-identify landed wrong metadata, correct it" case (post-rip
	// status). The submit endpoint is the same; only the copy differs.
	const isEditMode = $derived(!isAwaitingIdentity(job.status));

	// CD-only: per-track count comes from the preserved scan_result on the
	// job's metadata_json. If it's absent we skip the per-track inputs and
	// show a helper line; the resolve still succeeds.
	const scanTrackCount = $derived(
		Array.isArray((job.metadata_json?.scan_result as { titles?: unknown[] } | undefined)?.titles)
			? ((job.metadata_json.scan_result as { titles: unknown[] }).titles.length as number)
			: 0
	);

	// Why automatic identification missed (one entry per skipped or failed
	// lookup), recorded at identify and cleared by a resolve.
	const missReasons = $derived(
		Array.isArray(job.metadata_json?.identify_miss_reasons)
			? (job.metadata_json.identify_miss_reasons as unknown[]).filter((r): r is string => typeof r === 'string')
			: []
	);

	// Video / DVD / BD / data fields.
	let title = $state(job.title ?? '');
	let year = $state<number | null>(job.year);

	// CD fields.
	let album = $state(job.title ?? '');
	let artist = $state('');
	let trackTitles = $state<string[]>(Array.from({ length: scanTrackCount }, () => ''));

	// Disc position (all discs) and season (video only). Nothing on a disc
	// records a season, and a box set's disc number often isn't in the label,
	// so these are the operator's way to fill the `{season}` / `{disc}` naming
	// tokens. Resolve keeps an omitted field as-is, so only a changed value is
	// sent (an emptied field goes as null, clearing it).
	let season = $state<number | null>(job.season ?? null);
	let discNumber = $state<number | null>(job.disc_number ?? null);
	let discTotal = $state<number | null>(job.disc_total ?? null);

	function numOrNull(v: number | string | null | undefined): number | null {
		if (v == null || v === '') return null;
		const n = Number(v);
		return Number.isFinite(n) ? n : null;
	}

	function changedPosition(): { season?: number | null; disc_number?: number | null; disc_total?: number | null } {
		const out: { season?: number | null; disc_number?: number | null; disc_total?: number | null } = {};
		if (!isCd && numOrNull(season) !== (job.season ?? null)) out.season = numOrNull(season);
		if (numOrNull(discNumber) !== (job.disc_number ?? null)) out.disc_number = numOrNull(discNumber);
		if (numOrNull(discTotal) !== (job.disc_total ?? null)) out.disc_total = numOrNull(discTotal);
		return out;
	}

	let submitting = $state(false);
	let error = $state<string | null>(null);

	// Search the configured metadata provider instead of typing everything in
	// (video discs only: a CD needs the artist/album/track shape this search
	// doesn't return). Picking a result fills Title and Year; on submit its
	// type and ids go to resolve, and its poster becomes the job's, the same
	// way TitleSearch's Apply does it. Retyping the title drops the pick.
	let searchQuery = $state(job.title ?? '');
	let searchType = $state<'movie' | 'tv'>(
		job.media_type === 'tv' || (job.media_type == null && job.looks_episodic) ? 'tv' : 'movie'
	);
	let searching = $state(false);
	let searchError = $state<string | null>(null);
	let candidates = $state<MetadataCandidate[]>([]);
	let picked = $state<MetadataCandidate | null>(null);

	async function runSearch(): Promise<void> {
		const q = searchQuery.trim();
		if (!q) return;
		searching = true;
		searchError = null;
		try {
			const resp = await searchMetadata(q, searchType);
			candidates = resp.candidates;
			if (resp.candidates.length === 0) searchError = 'No results found. Try a different search term.';
		} catch (e) {
			candidates = [];
			searchError = e instanceof Error ? e.message : 'Search failed';
		} finally {
			searching = false;
		}
	}

	function onSearchKeydown(e: KeyboardEvent): void {
		if (e.key === 'Enter') {
			// Enter searches; it must not submit the identify form.
			e.preventDefault();
			void runSearch();
		}
	}

	function pick(c: MetadataCandidate): void {
		picked = c;
		title = c.title;
		year = c.year ?? null;
	}

	// The pick, while the form still describes it.
	function currentPick(): MetadataCandidate | null {
		return picked && picked.title === title.trim() ? picked : null;
	}

	function pickedIdentity(c: MetadataCandidate | null): {
		media_type?: 'movie' | 'tv';
		external_ids?: Record<string, string>;
	} {
		if (c === null) return {};
		// Resolve reads an explicit null as "clear this id": send only the ids it has.
		const ids = Object.entries(c.external_ids ?? {}).filter((e): e is [string, string] => e[1] != null);
		return {
			media_type: c.kind === 'series' || c.kind === 'tv' ? 'tv' : 'movie',
			...(ids.length ? { external_ids: Object.fromEntries(ids) } : {})
		};
	}

	const canSubmit = $derived(
		submitting ? false : isCd ? album.trim().length > 0 && artist.trim().length > 0 : title.trim().length > 0
	);

	async function submit(event: Event): Promise<void> {
		event.preventDefault();
		if (!canSubmit) return;
		submitting = true;
		error = null;
		try {
			const music = isCd
				? {
						artist: artist.trim(),
						album: album.trim(),
						tracks: trackTitles.map((t) => ({ title: t.trim() }))
					}
				: undefined;
			const chosen = isCd ? null : currentPick();
			const resp = await resolveJob(job.id, {
				title: isCd ? album.trim() : title.trim(),
				year: year ?? null,
				music,
				...pickedIdentity(chosen),
				...changedPosition()
			});
			if (chosen?.poster_url && chosen.poster_url !== (job.poster_url_manual ?? null)) {
				try {
					await updateJobTitle(job.id, { poster_url_manual: chosen.poster_url });
				} catch {
					// The disc is identified; a poster that didn't save isn't worth failing it.
				}
			}
			onidentified(resp);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Identify failed';
		} finally {
			submitting = false;
		}
	}
</script>

{#snippet positionFields()}
	<div class="mb-1 flex gap-3">
		{#if !isCd}
			<label class="field flex-1">
				<span class="field-label">Season</span>
				<input
					bind:value={season}
					type="number"
					min="0"
					max="999"
					data-testid="identify-season"
					disabled={submitting}
				/>
			</label>
		{/if}
		<label class="field flex-1">
			<span class="field-label">Disc #</span>
			<input
				bind:value={discNumber}
				type="number"
				min="1"
				max="999"
				data-testid="identify-disc-number"
				disabled={submitting}
			/>
		</label>
		<label class="field flex-1">
			<span class="field-label">Disc total</span>
			<input
				bind:value={discTotal}
				type="number"
				min="1"
				max="999"
				data-testid="identify-disc-total"
				disabled={submitting}
			/>
		</label>
	</div>
	<p class="field-help mb-3">
		{#if isCd}
			Multi-disc sets only: fills the <code class="mono">{'{disc}'}</code> naming token. Leave blank for a single disc.
		{:else}
			TV box sets and multi-disc releases: fills the <code class="mono">{'{season}'}</code> and
			<code class="mono">{'{disc}'}</code> naming tokens. Leave blank for a single disc.
		{/if}
	</p>
{/snippet}

<div class="modal">
	<button type="button" class="identify-dialog-backdrop" aria-label="Close dialog" onclick={onclose}></button>

	<div
		class="modal-panel identify-dialog-panel"
		data-dialog
		role="dialog"
		aria-modal="true"
		aria-labelledby="identify-dialog-title"
	>
		<h3 id="identify-dialog-title" class="modal-title">
			{isEditMode ? 'Edit identity' : 'Identify this disc'}
		</h3>

		{#if error}
			<p class="field-error mt-2" data-testid="identify-error">
				{error}
			</p>
		{/if}

		{#if isEditMode}
			<p class="modal-body">
				Update the title, year, and metadata for this job. Status stays as-is. Existing transcoded files keep their
				original filenames - re-apply a session if you want new outputs under the corrected name.
			</p>
		{:else}
			<p class="modal-body">
				The disc on drive <code class="mono">{driveLabel(job.drive_id, driveNames)}</code> couldn't be identified automatically.
				Fill in the details so ARM can proceed. Any session you've already applied will pick up the resolved metadata and
				queue its transcode tasks.
			</p>
		{/if}

		{#if missReasons.length > 0}
			<div class="alert alert-warning mt-3" data-testid="identify-miss-reasons">
				<div class="alert-title">Why automatic identification missed</div>
				<ul class="alert-body identify-dialog-miss-list">
					{#each missReasons as reason, i (i)}
						<li class="mono">{reason}</li>
					{/each}
				</ul>
			</div>
		{/if}

		{#if !isCd}
			<div class="identify-dialog-search mt-4" data-testid="identify-search">
				<div class="flex flex-wrap items-end gap-2">
					<label class="field identify-dialog-field-wide">
						<span class="field-label">Search title</span>
						<input
							bind:value={searchQuery}
							type="text"
							data-testid="identify-search-query"
							disabled={submitting || searching}
							onkeydown={onSearchKeydown}
						/>
					</label>
					<label class="field flex-1">
						<span class="field-label">Type</span>
						<select bind:value={searchType} data-testid="identify-search-type" disabled={submitting || searching}>
							<option value="movie">Movie</option>
							<option value="tv">TV</option>
						</select>
					</label>
					<button
						type="button"
						class="btn"
						data-testid="identify-search-run"
						onclick={runSearch}
						disabled={submitting || searching || !searchQuery.trim()}
					>
						{searching ? 'Searching...' : 'Search'}
					</button>
				</div>
				{#if searchError}
					<p class="field-error mt-2" data-testid="identify-search-error">{searchError}</p>
				{/if}
				{#if candidates.length > 0}
					<div class="identify-dialog-candidates mt-3">
						{#each candidates as c, i (`${c.provider_id ?? i}-${c.title}-${c.year ?? ''}`)}
							<button
								type="button"
								class="identify-dialog-candidate"
								data-testid="identify-candidate"
								aria-pressed={picked === c}
								disabled={submitting}
								onclick={() => pick(c)}
							>
								<PosterImage url={c.poster_url} alt={c.title} class="identify-dialog-candidate-poster" />
								<span class="identify-dialog-candidate-title">
									{c.title}{#if c.year}&nbsp;({c.year}){/if}
								</span>
							</button>
						{/each}
					</div>
					<p class="field-help mt-2">Pick one to fill in the title and year, then check them below.</p>
				{/if}
			</div>
		{/if}

		<form class="mt-4" onsubmit={submit}>
			{#if isCd}
				<div class="mb-3 flex gap-3">
					<label class="field identify-dialog-field-wide">
						<span class="field-label">Album</span>
						<input bind:value={album} type="text" required data-testid="identify-album" disabled={submitting} />
					</label>
					<label class="field flex-1">
						<span class="field-label">Year</span>
						<input
							bind:value={year}
							type="number"
							min="1888"
							max="2100"
							data-testid="identify-year"
							disabled={submitting}
						/>
					</label>
				</div>
				<div class="mb-3">
					<label class="field">
						<span class="field-label">Artist</span>
						<input bind:value={artist} type="text" required data-testid="identify-artist" disabled={submitting} />
					</label>
				</div>
				{#if scanTrackCount > 0}
					<div class="mb-3">
						<div class="field-label mb-1">Track titles</div>
						{#each trackTitles as _t, idx (idx)}
							<div class="mb-1 flex items-center gap-2">
								<span class="w-8 identify-dialog-track-index">
									{String(idx + 1).padStart(2, '0')}
								</span>
								<input
									bind:value={trackTitles[idx]}
									type="text"
									data-testid={`identify-track-${idx + 1}`}
									disabled={submitting}
									class="field-control flex-1"
								/>
							</div>
						{/each}
					</div>
				{:else}
					<p class="field-help mb-3">
						Track count couldn't be determined from the scan; transcoded filenames will fall back to generic names.
					</p>
				{/if}
				{@render positionFields()}
			{:else}
				<div class="mb-3 flex gap-3">
					<label class="field identify-dialog-field-wide">
						<span class="field-label">Title</span>
						<input bind:value={title} type="text" required data-testid="identify-title" disabled={submitting} />
					</label>
					<label class="field flex-1">
						<span class="field-label">Year</span>
						<input
							bind:value={year}
							type="number"
							min="1888"
							max="2100"
							data-testid="identify-year"
							disabled={submitting}
						/>
					</label>
				</div>
				{@render positionFields()}
			{/if}

			<div class="modal-actions">
				<button type="button" onclick={onclose} disabled={submitting} class="btn"> Cancel </button>
				<button type="submit" disabled={!canSubmit} data-testid="identify-submit" class="btn btn-primary">
					{submitting ? 'Saving...' : isEditMode ? 'Edit identity' : 'Identify disc'}
				</button>
			</div>
		</form>
	</div>
</div>

<style>
	.identify-dialog-track-index {
		text-align: right;
		font-size: 0.875rem;
		line-height: 1.25rem;
		color: var(--color-text-muted);
	}
	/* the backdrop click-catcher sits absolutely inside .modal (which already
	   draws the fixed scrim + centering) and fills that box. The panel must
	   stack above it: a positioned element paints over an unpositioned one,
	   so without this every click in the form landed on the backdrop and
	   closed the dialog (same pattern as ConfirmDialog / ApplySessionDialog). */
	.identify-dialog-backdrop {
		position: absolute;
		inset: 0;
		z-index: 0;
	}
	.identify-dialog-panel {
		position: relative;
		z-index: 1;
	}
	.identify-dialog-field-wide {
		flex: 2 1 0%;
	}
	.identify-dialog-search {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		padding: 0.75rem;
	}
	.identify-dialog-candidates {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(6rem, 1fr));
		gap: 0.5rem;
		max-height: 18rem;
		overflow-y: auto;
	}
	.identify-dialog-candidate {
		display: flex;
		flex-direction: column;
		overflow: hidden;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		background: var(--color-surface);
		text-align: left;
		cursor: pointer;
	}
	.identify-dialog-candidate:hover {
		border-color: var(--color-border-strong);
	}
	.identify-dialog-candidate[aria-pressed='true'] {
		border-color: var(--color-primary);
		box-shadow: 0 0 0 2px var(--color-primary-tint-1);
	}
	.identify-dialog-candidate :global(.identify-dialog-candidate-poster) {
		aspect-ratio: 2 / 3;
		width: 100%;
		object-fit: cover;
	}
	.identify-dialog-candidate-title {
		padding: 0.375rem;
		font-size: 0.75rem;
		line-height: 1rem;
		font-weight: 500;
		color: var(--color-text);
	}
	.identify-dialog-miss-list {
		margin: 0.25rem 0 0;
		padding-left: 1.125rem;
		list-style: disc;
		font-size: 0.75rem;
		overflow-wrap: anywhere;
	}
</style>
