<script lang="ts">
	import { resolveJob, searchMetadata } from '$lib/api/jobs';
	import type { JobView, MetadataCandidate, ResolveResponse } from '$lib/types/api.gen';
	import PosterImage from './PosterImage.svelte';

	let {
		job,
		onclose,
		onidentified
	}: {
		job: JobView;
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
	const isEditMode = $derived(
		!['awaiting_user_id', 'ripped_awaiting_identify'].includes(job.status)
	);

	// CD-only: per-track count comes from the preserved scan_result on the
	// job's metadata_json. If it's absent we skip the per-track inputs and
	// show a helper line; the resolve still succeeds.
	const scanTrackCount = $derived(
		Array.isArray(
			(job.metadata_json?.scan_result as { titles?: unknown[] } | undefined)?.titles
		)
			? ((job.metadata_json.scan_result as { titles: unknown[] }).titles.length as number)
			: 0
	);

	// Video / DVD / BD / data fields.
	let title = $state(job.title ?? '');
	let year = $state<number | null>(job.year);

	// Season / disc are pure operator input - MakeMKV surfaces no season or disc
	// signal and no metadata provider fills them in - and they are the only source
	// the `{season}` / `{disc}` naming tokens have at apply time. Without them a
	// box set applied against the built-in TV session fails with "token {disc}
	// resolved empty". Seeded from the job because resolve overwrites all three
	// unconditionally: submitting without them would clear what was set earlier.
	let seasonNumber = $state<number | null>(job.season_number ?? null);
	let discNumber = $state<number | null>(job.disc_number ?? null);
	let discTotal = $state<number | null>(job.disc_total ?? null);

	// A cleared number input binds as null/undefined/''; the API wants null.
	function numOrNull(v: number | string | null | undefined): number | null {
		if (v === '' || v == null) return null;
		const n = Number(v);
		return Number.isFinite(n) ? n : null;
	}

	// CD fields.
	let album = $state(job.title ?? '');
	let artist = $state('');
	let trackTitles = $state<string[]>(Array.from({ length: scanTrackCount }, () => ''));

	let submitting = $state(false);
	let error = $state<string | null>(null);

	// Manual title search against the configured metadata provider. Mitigates
	// auto-identify misses from a noisy volume label (a disc/volume-set token can
	// make the automatic query miss entirely). Video path only: the CD path needs
	// a structured artist/album/track shape this candidate search doesn't return.
	let searchTitle = $state(job.title ?? '');
	let searchType = $state<'movie' | 'tv'>('movie');
	let searching = $state(false);
	let searchError = $state<string | null>(null);
	let candidates = $state<MetadataCandidate[]>([]);
	let pickedCandidate = $state<MetadataCandidate | null>(null);

	async function doSearch(): Promise<void> {
		const q = searchTitle.trim();
		if (!q) return;
		searching = true;
		searchError = null;
		try {
			const resp = await searchMetadata(q, searchType);
			candidates = resp.candidates;
			searchError = resp.candidates.length === 0 ? (resp.detail ?? 'No results') : null;
		} catch (e) {
			candidates = [];
			searchError = e instanceof Error ? e.message : 'Search failed';
		} finally {
			searching = false;
		}
	}

	function onSearchKeydown(e: KeyboardEvent): void {
		if (e.key === 'Enter') {
			e.preventDefault();
			void doSearch();
		}
	}

	function pickCandidate(c: MetadataCandidate): void {
		title = c.title;
		year = c.year ?? null;
		pickedCandidate = c;
	}

	// The picked candidate's identity rides along as metadata - provider id,
	// kind, and its poster (resolve promotes `metadata.poster_url` onto the job's
	// poster, so correcting a mis-identified disc also swaps the wrong artwork).
	// Only while the title still matches the pick: once the operator retypes the
	// title, the candidate no longer describes what they're submitting.
	function candidateMetadata(): Record<string, unknown> | undefined {
		const c = pickedCandidate;
		if (!c || c.title !== title.trim()) return undefined;
		return { provider_id: c.provider_id ?? null, poster_url: c.poster_url ?? null, kind: c.kind };
	}

	const canSubmit = $derived(
		submitting
			? false
			: isCd
				? album.trim().length > 0 && artist.trim().length > 0
				: title.trim().length > 0
	);

	const inputClass =
		'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-white';
	const labelClass = 'text-sm font-medium text-gray-700 dark:text-gray-300';
	const hintClass = 'mb-3 text-xs text-gray-500 dark:text-gray-400';

	async function submit(event: Event): Promise<void> {
		event.preventDefault();
		if (!canSubmit) return;
		submitting = true;
		error = null;
		try {
			const metadata = isCd
				? {
						artist: artist.trim(),
						album: album.trim(),
						tracks: trackTitles.map((t) => ({ title: t.trim() }))
					}
				: candidateMetadata();
			const resp = await resolveJob(job.id, {
				title: isCd ? album.trim() : title.trim(),
				year: numOrNull(year),
				// Sent on both paths: resolve assigns these unconditionally. A CD has
				// no season, so it always sends null there.
				season_number: isCd ? null : numOrNull(seasonNumber),
				disc_number: numOrNull(discNumber),
				disc_total: numOrNull(discTotal),
				metadata
			});
			onidentified(resp);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Identify failed';
		} finally {
			submitting = false;
		}
	}
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center">
	<button
		type="button"
		class="absolute inset-0 bg-black/50"
		aria-label="Close dialog"
		onclick={onclose}
	></button>

	<div
		class="relative z-10 max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-lg bg-surface p-6 shadow-xl dark:bg-surface-dark"
		data-dialog
		role="dialog"
		aria-modal="true"
		aria-labelledby="identify-dialog-title"
	>
		<h3
			id="identify-dialog-title"
			class="text-lg font-semibold text-gray-900 dark:text-white"
		>
			{isEditMode ? 'Edit identity' : 'Identify this disc'}
		</h3>

		{#if error}
			<p class="mt-2 text-sm text-red-600 dark:text-red-400" data-testid="identify-error">
				{error}
			</p>
		{/if}

		{#if isEditMode}
			<p class="mt-2 text-sm text-gray-600 dark:text-gray-400">
				Update the title, year, and metadata for this job. Status stays as-is. Existing
				transcoded files keep their original filenames - re-apply a session if you want new
				outputs under the corrected name.
			</p>
		{:else}
			<p class="mt-2 text-sm text-gray-600 dark:text-gray-400">
				The disc on drive <code class="font-mono">{job.drive_id}</code> couldn't be identified
				automatically. Fill in the details so ARM can proceed. Any session you've already applied
				will pick up the resolved metadata and queue its transcode tasks.
			</p>
		{/if}

		{#if !isCd}
			<div
				class="mt-4 rounded-lg border border-primary/20 p-3 dark:border-primary/20"
				data-testid="identify-search"
			>
				<div class="flex flex-wrap items-end gap-2">
					<label class="min-w-[10rem] flex-[2] {labelClass}">
						Search title
						<input
							bind:value={searchTitle}
							type="text"
							data-testid="search-title"
							disabled={submitting || searching}
							onkeydown={onSearchKeydown}
							class="mt-1 {inputClass}"
						/>
					</label>
					<label class="flex-1 {labelClass}">
						Type
						<select
							bind:value={searchType}
							data-testid="search-type"
							disabled={submitting || searching}
							class="mt-1 {inputClass}"
						>
							<option value="movie">Movie</option>
							<option value="tv">TV</option>
						</select>
					</label>
					<button
						type="button"
						data-testid="search-run"
						onclick={doSearch}
						disabled={searching || submitting || !searchTitle.trim()}
						class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-on-primary hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
					>
						{searching ? 'Searching...' : 'Search'}
					</button>
				</div>
				{#if searchError}
					<p class="mt-2 text-sm text-red-600 dark:text-red-400" data-testid="search-error">
						{searchError}
					</p>
				{/if}
				{#if candidates.length > 0}
					<div class="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-4">
						{#each candidates as c, i (`${c.provider_id ?? i}-${c.title}-${c.year ?? ''}`)}
							<button
								type="button"
								data-testid="identify-candidate-{c.provider_id ?? i}"
								aria-pressed={pickedCandidate === c}
								onclick={() => pickCandidate(c)}
								disabled={submitting}
								class="flex flex-col overflow-hidden rounded-lg border text-left transition-all {pickedCandidate === c
									? 'border-primary ring-2 ring-primary/30'
									: 'border-primary/20 hover:border-primary/40 dark:border-primary/20 dark:hover:border-primary/40'}"
							>
								<PosterImage
									url={c.poster_url}
									alt={c.title}
									class="aspect-[2/3] w-full object-cover"
								/>
								<span class="line-clamp-2 p-1.5 text-xs font-medium text-gray-900 dark:text-white">
									{c.title}{#if c.year}&nbsp;({c.year}){/if}
								</span>
							</button>
						{/each}
					</div>
				{/if}
			</div>
		{/if}

		<form class="mt-4" onsubmit={submit}>
			{#if isCd}
				<div class="mb-3 flex gap-3">
					<label class="flex-[2] {labelClass}">
						Album
						<input
							bind:value={album}
							type="text"
							required
							data-testid="identify-album"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
					<label class="flex-1 {labelClass}">
						Year
						<input
							bind:value={year}
							type="number"
							min="1888"
							max="2100"
							data-testid="identify-year"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
				</div>
				<div class="mb-3">
					<label class={labelClass}>
						Artist
						<input
							bind:value={artist}
							type="text"
							required
							data-testid="identify-artist"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
				</div>
				<div class="mb-1 flex gap-3">
					<label class="flex-1 {labelClass}">
						Disc #
						<input
							bind:value={discNumber}
							type="number"
							min="1"
							placeholder="—"
							data-testid="identify-disc-number"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
					<label class="flex-1 {labelClass}">
						Disc total
						<input
							bind:value={discTotal}
							type="number"
							min="1"
							placeholder="—"
							data-testid="identify-disc-total"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
				</div>
				<p class={hintClass}>
					Multi-disc sets only - the source of the <code>{'{disc}'}</code> naming token when the
					MusicBrainz match doesn't supply one. Leave blank for a single disc.
				</p>
				{#if scanTrackCount > 0}
					<div class="mb-3">
						<div class="mb-1 text-sm text-gray-500 dark:text-gray-400">Track titles</div>
						{#each trackTitles as _t, idx (idx)}
							<div class="mb-1 flex items-center gap-2">
								<span class="w-8 text-right text-sm text-gray-500 dark:text-gray-400">
									{String(idx + 1).padStart(2, '0')}
								</span>
								<input
									bind:value={trackTitles[idx]}
									type="text"
									data-testid={`identify-track-${idx + 1}`}
									disabled={submitting}
									class="flex-1 {inputClass}"
								/>
							</div>
						{/each}
					</div>
				{:else}
					<p class="mb-3 text-sm text-gray-500 dark:text-gray-400">
						Track count couldn't be determined from the scan; transcoded filenames will fall back
						to generic names.
					</p>
				{/if}
			{:else}
				<div class="mb-3 flex gap-3">
					<label class="flex-[2] {labelClass}">
						Title
						<input
							bind:value={title}
							type="text"
							required
							data-testid="identify-title"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
					<label class="flex-1 {labelClass}">
						Year
						<input
							bind:value={year}
							type="number"
							min="1888"
							max="2100"
							data-testid="identify-year"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
				</div>
				<div class="mb-1 flex gap-3">
					<label class="flex-1 {labelClass}">
						Season
						<input
							bind:value={seasonNumber}
							type="number"
							min="0"
							placeholder="—"
							data-testid="identify-season-number"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
					<label class="flex-1 {labelClass}">
						Disc #
						<input
							bind:value={discNumber}
							type="number"
							min="1"
							placeholder="—"
							data-testid="identify-disc-number"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
					<label class="flex-1 {labelClass}">
						Disc total
						<input
							bind:value={discTotal}
							type="number"
							min="1"
							placeholder="—"
							data-testid="identify-disc-total"
							disabled={submitting}
							class="mt-1 {inputClass}"
						/>
					</label>
				</div>
				<p class={hintClass}>
					TV box sets only. Nothing on the disc records these, so sessions whose template uses
					<code>{'{season}'}</code> or <code>{'{disc}'}</code> can't be applied until you fill them
					in. Leave blank for a film.
				</p>
			{/if}

			<div class="mt-4 flex justify-end gap-3">
				<button
					type="button"
					onclick={onclose}
					disabled={submitting}
					class="rounded-lg px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
				>
					Cancel
				</button>
				<button
					type="submit"
					disabled={!canSubmit}
					data-testid="identify-submit"
					class="rounded-lg px-4 py-2 text-sm font-medium confirm-btn-primary disabled:cursor-not-allowed disabled:opacity-50"
				>
					{submitting ? 'Saving...' : isEditMode ? 'Edit identity' : 'Identify disc'}
				</button>
			</div>
		</form>
	</div>
</div>
