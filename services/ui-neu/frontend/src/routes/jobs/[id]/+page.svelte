<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { onMount, tick } from 'svelte';
	import { keepTracks, retryFailedTitles } from '$lib/api/jobs';
	import { jobPoster } from '$lib/utils/poster';
	import PosterImage from '$lib/components/PosterImage.svelte';
	import type {
		MenuProposalView,
		ResolveResponse,
		ApplySessionResponse,
		SubtitleOcrResponse
	} from '$lib/types/api.gen';
	import JobFactsGrid from '$lib/components/JobFactsGrid.svelte';
	import type { EditableJobField } from '$lib/utils/job-fields';
	import JobHeaderActions from '$lib/components/JobHeaderActions.svelte';
	import JobTitleBar from '$lib/components/JobTitleBar.svelte';
	import JobTracksTable from '$lib/components/JobTracksTable.svelte';
	import IdentityPanel from '$lib/components/identity/IdentityPanel.svelte';
	import JobFingerprints from '$lib/components/JobFingerprints.svelte';
	import JobMusicTracklist from '$lib/components/JobMusicTracklist.svelte';
	import IdentifyDialog from '$lib/components/IdentifyDialog.svelte';
	import ApplySessionDialog from '$lib/components/ApplySessionDialog.svelte';
	import JobLifecycle from '$lib/components/JobLifecycle.svelte';
	import ReviewGatePanel from '$lib/components/ReviewGatePanel.svelte';
	import JobTrackPicker from '$lib/components/JobTrackPicker.svelte';
	import RawBackupRow from '$lib/components/RawBackupRow.svelte';
	import { identitySuggestionOf } from '$lib/utils/job-review';
	import { effectiveJobStatus, isPartialComplete } from '$lib/utils/job-status';
	import IdentitySuggestionBanner from '$lib/components/IdentitySuggestionBanner.svelte';
	import { isLive, isPreRipStatus, isRipperBusyStatus } from '$lib/utils/job-status-groups';
	import {
		isDiscMenu,
		isRetryArmed,
		keepAllIds,
		choosingTitles,
		CHOSEN_AFTER_BACKUP,
		keepSummary,
		titleTracks
	} from '$lib/utils/track-keep';
	import { applyNote, identifyNote } from '$lib/utils/job-action-notes';
	import { fetchSessions } from '$lib/api/sessions';
	import { buildMetadataFields } from '$lib/utils/job-fields';
	import LoadState from '$lib/components/LoadState.svelte';
	import SkeletonCard from '$lib/components/SkeletonCard.svelte';
	import JobRawMetadata from '$lib/components/JobRawMetadata.svelte';
	import JobLogPanel from '$lib/components/JobLogPanel.svelte';
	import DiscMenuPanel from '$lib/components/DiscMenuPanel.svelte';
	import { acceptAllMenuProposals } from '$lib/api/discMenus';
	import { acceptableProposals, proposalByTrack } from '$lib/utils/disc-menu';
	import { EpisodeMatching, watchEpisodeMatching } from '$lib/stores/episodeMatching.svelte';
	import { reconcileSubscriptions, startWS } from '$lib/stores/rips.svelte';
	import JobRipProgress from '$lib/components/JobRipProgress.svelte';
	import { isAdmin } from '$lib/stores/auth';
	import { dashboard } from '$lib/stores/dashboard';
	import { JobDetail } from '$lib/stores/jobDetail.svelte';

	const jobDetail = new JobDetail(() => $page.params.id ?? '');
	let detail = $derived(jobDetail.detail);
	// The Identity panel (search, season and disc, episodes; MusicSearch on a CD).
	let identityOpen = $state(false);
	let identityPanel = $state<
		| { reloadEpisodes: () => Promise<void>; showSearch: (type?: 'movie' | 'tv') => void; accepted: () => void }
		| undefined
	>(undefined);

	// "Matching episodes..." while the Backend re-matches after a type flip or a
	// series apply; job.identity_updated for this job (or a timeout) ends it.
	const matching = new EpisodeMatching();
	// How long a title's row stays highlighted after a menu box jumps to it.
	const FLASH_MS = 4000;
	// Why the last Keep change was refused (shown above the Tracks table).
	let trackError = $state<string | null>(null);

	// Dialog visibility + a transient note shown after a resolve/apply lands.
	let showIdentify = $state(false);
	let showApply = $state(false);
	let actionNote = $state<string | null>(null);
	let noteTimer: ReturnType<typeof setTimeout> | null = null;

	// A disc with an Identity panel (video, CD) opens that instead of the dialog.
	function openIdentity() {
		if (!hasIdentityPanel) {
			showIdentify = true;
			return;
		}
		identityOpen = true;
		requestAnimationFrame(() =>
			document.getElementById('identity-panel')?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
		);
	}

	function flashNote(message: string) {
		actionNote = message;
		if (noteTimer) clearTimeout(noteTimer);
		noteTimer = setTimeout(() => {
			actionNote = null;
		}, 6000);
	}

	function handleIdentified(resp: ResolveResponse) {
		showIdentify = false;
		identityPanel?.accepted();
		jobDetail.load();
		const note = identifyNote(resp);
		if (note) flashNote(note);
	}

	function handleApplied(resp: ApplySessionResponse) {
		showApply = false;
		jobDetail.load();
		flashNote(applyNote(resp, jobDetail.isPostRip));
	}

	function handleTextSubtitles(resp: SubtitleOcrResponse) {
		jobDetail.load();
		const n = resp.tasks?.length ?? 0;
		const running = (resp.skipped ?? []).filter((s) => s.reason === 'running').length;
		if (n > 0) flashNote(`Text subtitles queued for ${n} encode${n === 1 ? '' : 's'}`);
		else if (running > 0) flashNote('Text subtitles are already being made');
		else flashNote('Every encode already has text subtitles');
	}

	let hasIdentityPanel = $derived(jobDetail.isVideoDisc || jobDetail.isCdDisc);

	let sessionNames = $state(new Map<string, string>());
	let metadataFields = $derived(detail ? buildMetadataFields(detail.job, $dashboard.drive_names, sessionNames) : []);

	// What the last Retry failed titles click started (shown under the Tracks table).
	let retryNote = $state<string | null>(null);
	let retrying = $state(false);

	// The disc menu's names, shown on the titles' rows (the Disc menu panel loads them).
	let menuPanel = $state<{ reload: () => Promise<void> } | undefined>(undefined);
	let menuProposals = $state<MenuProposalView[]>([]);
	let menuByTrack = $derived(proposalByTrack(menuProposals));
	let menuAcceptable = $derived(acceptableProposals(menuProposals));
	let acceptingAll = $state(false);
	let menuNote = $state<string | null>(null);

	function menuNameChanged(updated: MenuProposalView) {
		menuProposals = menuProposals.map((p) => (p.id === updated.id ? updated : p));
		if (updated.status === 'applied' || updated.status === 'accepted') jobDetail.load();
		void menuPanel?.reload();
	}

	function menuProposalsLoaded(list: MenuProposalView[]) {
		menuProposals = list;
		// A finished preview adds rows for the short titles the menu plays: load them.
		const have = new Set((detail?.tracks ?? []).map((t) => t.id));
		if (list.some((p) => p.track_id && !have.has(p.track_id))) jobDetail.load();
	}

	// A title's menu-name row: open when the menu names it, else when its Episode cell was clicked. A click on the
	// cell flips it either way.
	let menuRowOverride = $state<Record<string, boolean>>({});
	let menuRowOpen = (trackId: string) => menuRowOverride[trackId] ?? menuByTrack.has(trackId);
	function toggleMenuRow(trackId: string) {
		menuRowOverride = { ...menuRowOverride, [trackId]: !menuRowOpen(trackId) };
	}

	function menuNamedByHand(created: MenuProposalView) {
		menuProposals = [...menuProposals, created];
		jobDetail.load();
		void menuPanel?.reload();
	}

	// A menu box was clicked: bring its title's row to the top and highlight it (and its menu name) for a moment.
	let flashTrackId = $state<string | null>(null);
	let flashTimer: ReturnType<typeof setTimeout> | null = null;
	async function focusTitle(trackId: string) {
		// Its menu name, if the menu names it (an episode's row opens nothing).
		if (menuByTrack.has(trackId)) menuRowOverride = { ...menuRowOverride, [trackId]: true };
		flashTrackId = trackId;
		if (flashTimer) clearTimeout(flashTimer);
		flashTimer = setTimeout(() => (flashTrackId = null), FLASH_MS);
		await tick();
		document.getElementById(`track-row-${trackId}`)?.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
	}

	async function acceptAllNames(jobId: string) {
		acceptingAll = true;
		menuNote = null;
		try {
			const res = await acceptAllMenuProposals(jobId);
			const refused = Object.keys(res.refused ?? {}).length;
			menuNote = refused ? `${refused} ${refused === 1 ? 'name was' : 'names were'} not accepted; see the rows.` : null;
			jobDetail.load();
		} catch (e) {
			menuNote = `Could not accept the names: ${e instanceof Error ? e.message : 'unknown error'}`;
		} finally {
			acceptingAll = false;
			void menuPanel?.reload();
		}
	}

	async function retryFailed(jobId: string) {
		retrying = true;
		trackError = null;
		retryNote = null;
		try {
			const res = await retryFailedTitles(jobId);
			const titles = res.retried === 1 ? '1 title' : `${res.retried} titles`;
			retryNote = res.from_backup
				? `Ripping ${titles} from the kept disc backup. No disc needed.`
				: detail?.retry_backs_up
					? 'Put the disc back in the drive: ARM backs it up and copies its menu as soon as it goes in.'
					: `Put the disc back in the drive: ARM rips ${titles} as soon as it goes in.`;
		} catch (e) {
			trackError = `Could not rip the remaining titles: ${e instanceof Error ? e.message : 'unknown error'}`;
		} finally {
			retrying = false;
			jobDetail.load();
		}
	}

	async function keepAll(trackIds: string[]) {
		trackError = null;
		try {
			await keepTracks($page.params.id ?? '', trackIds);
		} catch (e) {
			trackError = `Could not update the tracks: ${e instanceof Error ? e.message : 'unknown error'}`;
		} finally {
			jobDetail.load();
		}
	}

	async function saveFact(field: EditableJobField | 'title', raw: string) {
		if (await jobDetail.saveFact(field, raw)) matching.start();
	}

	function handleSeriesApplied() {
		matching.start();
		jobDetail.load();
	}

	function handleMediaTypeChanged(type: 'movie' | 'tv') {
		if (type === 'tv') {
			// Episode matching starts: show its step.
			matching.start();
			identityOpen = true;
		} else matching.stop();
		jobDetail.load();
	}

	// Client-side navigation from one job page to another keeps this component
	// mounted, so onMount never re-runs: reload whenever the route's job id changes
	// (a notification link, or Back/Forward between two jobs).
	let loadedId = $page.params.id ?? '';
	$effect(() => {
		const id = $page.params.id ?? '';
		if (id && id !== loadedId) {
			loadedId = id;
			jobDetail.load();
		}
	});

	// Live rip progress for this job (the same ripper.progress feed the
	// dashboard uses): subscribed only while the job is ripping or remuxing.
	let ripping = $derived(isRipperBusyStatus(detail?.job.status));
	$effect(() => {
		const id = detail?.job.id;
		reconcileSubscriptions(ripping && id ? [id] : []);
	});

	onMount(() => {
		let stopped = false;
		startWS();
		fetchSessions()
			.then((s) => (sessionNames = new Map(s.map((x) => [x.id, x.name]))))
			.catch(() => {});
		// Instant status for the job being viewed (ripper and transcode
		// events): refresh only when an event names this job, so other jobs'
		// events don't disturb the page. The 5s poll below stays as
		// reconciliation. Only the identity update ends matching
		// (rip.identify_resolved fires before the episode stage has run).
		const offRipperEvents = watchEpisodeMatching(
			() => $page.params.id,
			matching,
			async () => {
				await jobDetail.load();
				await identityPanel?.reloadEpisodes();
			}
		);
		async function poll() {
			// Never exits while mounted: a finished job can go live again (a new
			// apply), and the next tick picks that up without a remount.
			while (!stopped) {
				await new Promise((r) => setTimeout(r, 5000));
				if (!stopped && detail && isLive(detail.job)) await jobDetail.load({ poll: true });
			}
		}
		jobDetail.load().then(() => poll());
		return () => {
			stopped = true;
			offRipperEvents();
			reconcileSubscriptions([]);
			matching.stop();
		};
	});
</script>

<svelte:head>
	<title>ARM - {detail?.job.title || 'Job Detail'}</title>
</svelte:head>

<LoadState data={jobDetail.detail} loading={jobDetail.loading} error={jobDetail.error}>
	{#snippet loadingSlot()}
		<SkeletonCard lines={6} />
	{/snippet}
	{#snippet ready(d)}
		{@const job = d.job}
		{@const tracks = d.tracks}
		{@const keptCount = titleTracks(tracks).filter((t) => !t.excluded).length}
		{@const preRip = isPreRipStatus(job.status)}
		{@const parks = d.parks_after_backup ?? false}
		{@const choosing = choosingTitles(job.status, parks)}
		{@const keepNoun = job.disc_type === 'cd' ? 'tracks' : 'titles'}
		{@const remaining = tracks.filter(
			(t) => !t.excluded && (t.status === 'failed' || (t.status === 'queued' && !t.output_path))
		)}
		{@const remainingTitles = titleTracks(remaining)}
		{@const onlyFailed = remaining.every((t) => t.status === 'failed')}
		{@const retryArmed = isRetryArmed(job.metadata_json)}
		{@const retryBacksUp = d.retry_backs_up ?? false}
		{@const hasMenu = tracks.some((t) => isDiscMenu(t) && t.status === 'done')}
		<div class="stack-lg stack">
			<!-- Breadcrumb -->
			<nav class="job-detail-breadcrumb">
				<a href="/" class="job-detail-breadcrumb-link">Dashboard</a>
				<span class="mx-1.5 job-detail-breadcrumb-sep">&rsaquo;</span>
				<span class="job-detail-breadcrumb-current">{job.title || 'Untitled'}</span>
			</nav>

			{#if actionNote}
				<div data-testid="action-note" class="job-detail-action-note">
					{actionNote}
				</div>
			{/if}

			<!-- Main header container -->
			<div class="job-detail-header-card">
				<!-- Title bar -->
				<JobTitleBar {job} {jobDetail} {hasIdentityPanel} onsavetitle={(raw) => saveFact('title', raw)}>
					<JobHeaderActions
						{job}
						{jobDetail}
						{hasIdentityPanel}
						subtitleOcr={d.subtitle_ocr}
						ontextsubtitles={handleTextSubtitles}
						onidentify={openIdentity}
						onapply={() => (showApply = true)}
						ondelete={() => goto('/')}
					/>
				</JobTitleBar>

				<!-- A confident match identify held for confirmation (auto-apply off). -->
				{#if identitySuggestionOf(job)}
					<div class="job-detail-suggestion">
						<IdentitySuggestionBanner
							{job}
							canAct={$isAdmin && jobDetail.canResolve}
							onaccepted={handleIdentified}
							onsearch={() => (showIdentify = true)}
						/>
					</div>
				{/if}

				<!-- Poster + Metadata grid -->
				<div class="flex flex-col sm:flex-row items-start">
					<!-- Poster -->
					<div class="shrink-0 job-detail-poster-cell" data-disc-type={job.disc_type}>
						<PosterImage url={jobPoster(job)} alt={job.title ?? 'Poster'} class="job-detail-poster" />
						{#if jobDetail.isVideoDisc && $isAdmin}
							<button
								type="button"
								class="job-detail-poster-change"
								data-testid="poster-change"
								onclick={() => identityPanel?.showSearch()}>Change poster</button
							>
						{/if}
					</div>

					<!-- Metadata grid -->
					<JobFactsGrid
						fields={metadataFields}
						canEdit={$isAdmin && jobDetail.canResolve && !hasIdentityPanel}
						onsave={saveFact}
					/>
				</div>

				{#if hasIdentityPanel}
					<IdentityPanel
						bind:this={identityPanel}
						bind:open={identityOpen}
						{job}
						{tracks}
						matching={matching.active}
						handSetCount={jobDetail.handSetCount}
						onchanged={() => jobDetail.load()}
						onseries={handleSeriesApplied}
						ontypechanged={handleMediaTypeChanged}
						onmanual={() => (showIdentify = true)}
					/>
				{/if}
			</div>

			<!-- Lifecycle widget: visual stage progression below header, above status bars -->
			<div class="job-detail-lifecycle-card">
				<JobLifecycle status={effectiveJobStatus(job)} sourceType={null} size="md" partial={isPartialComplete(job)} />
			</div>

			{#if retryBacksUp}
				<!-- The backup failed, then so did the titles ripped from the disc: nothing was ripped (fork only). -->
				<section class="job-detail-lifecycle-card stack" data-testid="retry-backup-card">
					<h2 class="job-detail-section-title">The disc backup failed</h2>
					<p class="job-detail-retry-note">
						Nothing was ripped. Retry backs the disc up again and copies its menu once the disc is back in the drive;
						its titles are chosen after that.
					</p>
					{#if trackError}
						<p class="alert alert-danger" role="alert">{trackError}</p>
					{/if}
					{#if retryNote}
						<p class="job-detail-retry-note" data-testid="retry-note">{retryNote}</p>
					{:else if retryArmed}
						<p class="job-detail-retry-note" data-testid="retry-armed">
							Waiting for the disc: put it back in the drive to back it up again.
						</p>
					{:else if $isAdmin}
						<div>
							<button
								type="button"
								class="btn btn-primary"
								data-testid="retry-backup"
								disabled={retrying}
								onclick={() => retryFailed(job.id)}
							>
								{retrying ? 'Working...' : 'Retry backup and menu'}
							</button>
						</div>
					{/if}
				</section>
			{/if}

			<!-- A held suggestion is confirmed in its banner first; accepting it opens the gate. -->
			{#if (job.status === 'awaiting_review' && !identitySuggestionOf(job)) || job.status === 'awaiting_titles'}
				<ReviewGatePanel
					{job}
					parksAfterBackup={parks}
					requiresConfirm={$dashboard.review_requires_confirm ?? false}
					waitSeconds={$dashboard.manual_wait_seconds ?? 60}
					paused={!$dashboard.ripping_enabled}
					keptCount={tracks.length > 0 ? keptCount : null}
					onchanged={() => jobDetail.load()}
				/>
			{/if}

			{#if (job.disc_type === 'dvd' || job.disc_type === 'bluray') && job.status !== 'awaiting_review'}
				<!-- A held disc picks its tracks in the review panel above, saved on Start. Closed until opened. -->
				<details class="job-detail-lifecycle-card job-detail-tracks" data-testid="job-tracks-section">
					<summary class="job-detail-section-title">Audio and subtitle tracks</summary>
					<JobTrackPicker jobId={job.id} variant="page" />
				</details>
			{/if}

			{#if ripping}
				<JobRipProgress jobId={job.id} />
			{/if}

			<!-- Disc menu (fork only): its pages, and the names its buttons give the titles (on their rows below) -->
			{#if hasMenu}
				<DiscMenuPanel
					bind:this={menuPanel}
					jobId={job.id}
					canEdit={$isAdmin}
					onproposals={menuProposalsLoaded}
					onfocus={focusTitle}
				/>
			{/if}

			<!-- Tracks -->
			{#if tracks.length > 0}
				<JobTracksTable
					{job}
					{tracks}
					tracksAreSeries={jobDetail.tracksAreSeries}
					previewByTrack={jobDetail.previewByTrack}
					ontitleapplied={() => jobDetail.load()}
					{parks}
					{choosing}
					{keptCount}
					onkeepchanged={() => jobDetail.load()}
					onkeeperror={(message) => (trackError = message)}
					{hasMenu}
					{menuByTrack}
					{menuRowOpen}
					ontogglemenurow={toggleMenuRow}
					onmenuchange={menuNameChanged}
					onmenunamed={menuNamedByHand}
					{flashTrackId}
				>
					{#snippet notice()}
						{#if trackError && !retryBacksUp}
							<p class="alert alert-danger mb-3" role="alert" data-testid="track-error">{trackError}</p>
						{/if}
					{/snippet}
					{#snippet footer()}
						<div class="job-detail-keep-footer">
							<span data-testid="keep-summary"
								>{parks && preRip ? `${CHOSEN_AFTER_BACKUP}.` : keepSummary(tracks, keepNoun)}</span
							>
							{#if $isAdmin && choosing && keepAllIds(tracks).length > 0}
								<button type="button" class="btn job-detail-keep-all" onclick={() => keepAll(keepAllIds(tracks))}>
									Keep all
								</button>
							{/if}
							{#if $isAdmin && menuAcceptable.length > 0}
								<button
									type="button"
									class="btn job-detail-keep-all"
									data-testid="dm-accept-all"
									disabled={acceptingAll}
									title="Give every title the name its menu button proposes"
									onclick={() => acceptAllNames(job.id)}
								>
									{acceptingAll ? 'Working...' : `Accept all menu names (${menuAcceptable.length})`}
								</button>
							{/if}
							{#if $isAdmin && (job.status === 'ripped' || job.status === 'ripped_partial' || job.status === 'failed') && remainingTitles.length > 0 && !retryArmed && !retryBacksUp}
								<button
									type="button"
									class="btn job-detail-keep-all"
									disabled={retrying}
									title="Rip the kept titles that have no file yet: from the kept disc backup if there is one, else when the disc goes back in"
									onclick={() => retryFailed(job.id)}
								>
									{retrying
										? 'Working...'
										: `${onlyFailed ? 'Retry failed titles' : 'Rip remaining titles'} (${remainingTitles.length})`}
								</button>
							{/if}
						</div>
						{#if menuNote}
							<p class="job-detail-retry-note" data-testid="dm-refused">{menuNote}</p>
						{/if}
						{#if retryBacksUp}
							<!-- Its retry's notes are in the card above. -->
						{:else if retryNote}
							<p class="job-detail-retry-note" data-testid="retry-note">{retryNote}</p>
						{:else if retryArmed && !isRipperBusyStatus(job.status) && !d.raw_backup?.complete}
							<p class="job-detail-retry-note" data-testid="retry-armed">
								Waiting for the disc: put it back in the drive to rip the remaining titles.
							</p>
						{/if}
						{#if d.raw_backup}
							<RawBackupRow
								jobId={d.job.id}
								backup={d.raw_backup}
								ripping={isRipperBusyStatus(d.job.status) || d.job.status === 'awaiting_titles'}
								canDelete={$isAdmin}
								ondeleted={() => jobDetail.load()}
							/>
						{/if}
					{/snippet}
				</JobTracksTable>
			{/if}

			<!-- Disc fingerprints (read-only) -->
			{#if d.fingerprints && d.fingerprints.length > 0}
				<JobFingerprints fingerprints={d.fingerprints} />
			{/if}

			<!-- Music tracklist (CD jobs; read-only). Fallback ONLY when there are no
		     real Track rows yet — once the disc has tracks (which carry the mapped
		     song titles), the Tracks table above is authoritative and this would
		     just duplicate it. Mirrors neu's single-section behavior. -->
			{#if tracks.length === 0 && jobDetail.musicTracks.length > 0}
				<JobMusicTracklist tracks={jobDetail.musicTracks} />
			{/if}

			<!-- Live, per-service job log (backend + ripper + transcode aggregated) -->
			<JobLogPanel jobId={job.id} {job} />

			<!-- Raw metadata (collapsible): the full metadata_json as a JSON tree, nothing hidden -->
			{#if jobDetail.rawMetadataPairs.length > 0}
				<JobRawMetadata pairs={jobDetail.rawMetadataPairs} />
			{/if}
		</div>

		{#if showIdentify}
			<IdentifyDialog
				{job}
				driveNames={$dashboard.drive_names}
				onclose={() => (showIdentify = false)}
				onidentified={handleIdentified}
			/>
		{/if}
		{#if showApply}
			<ApplySessionDialog {job} onclose={() => (showApply = false)} onapplied={handleApplied} />
		{/if}
	{/snippet}
</LoadState>

<style>
	.job-detail-breadcrumb {
		font-size: 0.875rem;
		line-height: 1.25rem;
		color: var(--color-text-muted);
	}
	.job-detail-breadcrumb-link {
		color: var(--color-primary-text);
	}
	.job-detail-breadcrumb-link:hover {
		text-decoration: underline;
	}
	.job-detail-breadcrumb-sep {
		color: var(--color-text-faint);
	}
	.job-detail-breadcrumb-current {
		color: var(--color-text-muted);
	}
	.job-detail-action-note {
		border: 1px solid var(--color-border-strong);
		border-radius: var(--radius-lg);
		background: var(--color-primary-tint-1);
		padding: 0.5rem 1rem;
		font-size: 0.875rem;
		line-height: 1.25rem;
		color: var(--color-primary-text);
	}
	.job-detail-header-card {
		overflow: hidden;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		background: var(--color-surface);
		box-shadow: var(--shadow-1);
	}
	.job-detail-suggestion {
		border-bottom: 1px solid var(--color-border);
		padding: 0.75rem 1.25rem;
	}
	.job-detail-poster-cell {
		border-bottom: 1px solid var(--color-border);
		padding: 1rem;
	}
	@media (min-width: 640px) {
		.job-detail-poster-cell {
			border-bottom: 0;
			border-right: 1px solid var(--color-border);
		}
	}
	/* :global: forwarded through PosterImage's class prop */
	:global(.job-detail-poster) {
		width: 120px;
		aspect-ratio: 2 / 3;
		border-radius: var(--radius-md);
		object-fit: cover;
		box-shadow: var(--shadow-1);
	}
	/* An audio CD's cover is square. :global: the poster is rendered by PosterImage. */
	.job-detail-poster-cell[data-disc-type='cd'] :global(.job-detail-poster) {
		aspect-ratio: 1 / 1;
	}
	.job-detail-poster-change {
		display: block;
		margin-top: 0.375rem;
		font-size: 0.75rem;
		color: var(--color-primary);
	}
	.job-detail-poster-change:hover {
		text-decoration: underline;
	}
	.job-detail-lifecycle-card {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		background: var(--color-surface);
		padding: 0.75rem 1rem;
		box-shadow: var(--shadow-1);
	}
	.job-detail-section-title {
		font-size: 1.125rem;
		line-height: 1.75rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.job-detail-tracks > summary {
		cursor: pointer;
	}
	.job-detail-tracks[open] > summary {
		margin-bottom: 1rem;
	}
	.job-detail-keep-footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.75rem;
		font-size: 0.875rem;
		color: var(--color-text-muted);
	}
	.job-detail-retry-note {
		margin-top: 0.5rem;
		font-size: 0.875rem;
		color: var(--color-text-muted);
	}
	.job-detail-keep-all {
		padding: 0.25rem 0.625rem;
		font-size: 0.75rem;
	}
</style>
