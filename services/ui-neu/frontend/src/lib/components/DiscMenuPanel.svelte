<script lang="ts">
	// Job page: the disc menu (fork only, disc menus M3-M4). Shows each menu
	// page the preview read, with every button's box over it, and the names the
	// menu's buttons propose for the job's titles. Accepting a name renames the
	// finished encode into the library's trailers/, other/, extras/ or Season 00
	// folder (an encode still running is renamed when it finishes). A title the
	// menu doesn't name can be named here by hand. Shown only when the job has a
	// copied disc menu (the GET 404s otherwise).
	import { onDestroy } from 'svelte';
	import {
		acceptAllMenuProposals,
		createMenuProposal,
		fetchDiscMenu,
		fetchDiscMenuImage,
		requestDiscMenuPreview,
		updateMenuProposal
	} from '$lib/api/discMenus';
	import { ApiError } from '$lib/api/client';
	import type {
		JobDiscMenuView,
		MenuPreviewButtonView,
		MenuProposalView,
		TrackRole,
		TrackView
	} from '$lib/types/api.gen';
	import {
		MENU_ROLES,
		PROPOSAL_STATUS,
		acceptBlocker,
		boxPercent,
		buttonHint,
		draftOf,
		isOpen,
		isPreviewLive,
		previewState,
		type ProposalDraft
	} from '$lib/utils/disc-menu';
	import { usePoll } from '$lib/utils/poll';
	import Glyph from './Glyph.svelte';

	interface Props {
		jobId: string;
		/** The job's tracks: names the titles a proposal plays, and the by-hand picker. */
		tracks: TrackView[];
		canEdit: boolean;
		/** Called after a rename, so the page can reload the tracks' new paths. */
		onrenamed?: () => void;
	}

	let { jobId, tracks, canEdit, onrenamed }: Props = $props();

	let view = $state<JobDiscMenuView | null>(null);
	let absent = $state(false);
	let error = $state<string | null>(null);
	let pageId = $state<string | null>(null);
	let focused = $state<string | null>(null);
	let busy = $state<string | null>(null);
	let drafts = $state<Record<string, ProposalDraft>>({});
	let rowErrors = $state<Record<string, string>>({});
	let refusedCount = $state(0);
	let images = $state<Record<string, string>>({});
	let byHand = $state(false);
	let handDraft = $state<ProposalDraft & { trackId: string }>({ trackId: '', role: null, name: '', episode: null });

	const pages = $derived(view?.preview?.pages ?? []);
	const page = $derived(pages.find((p) => p.id === pageId) ?? pages[0] ?? null);
	const proposals = $derived(view?.proposals ?? []);
	const named = $derived(proposals.filter((p) => p.status !== 'info'));
	const notes = $derived(proposals.filter((p) => p.status === 'info'));
	const acceptable = $derived(named.filter((p) => p.status === 'proposed' && !acceptBlocker(p, draft(p))));
	const titles = $derived(tracks.filter((t) => t.kind === 'video_title' && t.status === 'done'));

	function trackName(id: string | null | undefined): string {
		const t = tracks.find((x) => x.id === id);
		return t ? `Title ${t.index}` : 'Not ripped';
	}

	function pageName(id: string): string {
		const p = pages.find((x) => x.id === id);
		return p ? `page ${p.id}` : `page ${id}`;
	}

	function draft(p: MenuProposalView): ProposalDraft {
		return drafts[p.id] ?? draftOf(p);
	}

	function edit(p: MenuProposalView, change: Partial<ProposalDraft>): void {
		drafts = { ...drafts, [p.id]: { ...draft(p), ...change } };
	}

	function message(e: unknown, fallback: string): string {
		return e instanceof Error && e.message ? e.message : fallback;
	}

	async function load(): Promise<void> {
		try {
			view = await fetchDiscMenu(jobId);
			error = null;
		} catch (e) {
			if (e instanceof ApiError && e.status === 404) absent = true;
			else error = message(e, 'Could not load the disc menu');
		}
	}

	$effect(() => {
		void jobId;
		void load();
	});

	// Poll while the preview runs; stop once it's done or failed.
	$effect(() => {
		if (!isPreviewLive(view?.preview)) return;
		let first = true;
		const poll = usePoll(async () => {
			if (first) {
				first = false;
				return;
			}
			await load();
		}, 4000);
		return () => poll.stop();
	});

	// Fetch each page image once, with the token (an <img> can't send it).
	$effect(() => {
		const p = page;
		if (!p || images[p.id]) return;
		fetchDiscMenuImage(jobId, p.image)
			.then((url) => (images = { ...images, [p.id]: url }))
			.catch(() => {
				/* the page shows its buttons without the picture */
			});
	});

	onDestroy(() => {
		for (const url of Object.values(images)) URL.revokeObjectURL(url);
	});

	function replace(updated: MenuProposalView): void {
		if (!view) return;
		const list = view.proposals ?? [];
		const has = list.some((p) => p.id === updated.id);
		view = { ...view, proposals: has ? list.map((p) => (p.id === updated.id ? updated : p)) : [...list, updated] };
		const { [updated.id]: _, ...rest } = drafts;
		drafts = rest;
	}

	async function act(p: MenuProposalView, action: 'accept' | 'reject' | 'reopen'): Promise<void> {
		busy = p.id;
		const { [p.id]: _, ...others } = rowErrors;
		rowErrors = others;
		try {
			const d = draft(p);
			const body =
				action === 'accept' ? { role: d.role, name: d.name.trim(), episode_number: d.episode, action } : { action };
			const updated = await updateMenuProposal(jobId, p.id, body);
			replace(updated);
			if (updated.status === 'applied') onrenamed?.();
		} catch (e) {
			rowErrors = { ...rowErrors, [p.id]: message(e, 'Could not save that') };
		} finally {
			busy = null;
		}
	}

	async function acceptAll(): Promise<void> {
		busy = 'all';
		error = null;
		try {
			const resp = await acceptAllMenuProposals(jobId);
			for (const p of resp.proposals) replace(p);
			const refused = resp.refused ?? {};
			rowErrors = { ...rowErrors, ...refused };
			refusedCount = Object.keys(refused).length;
			if (resp.proposals.some((p) => p.status === 'applied')) onrenamed?.();
		} catch (e) {
			error = message(e, 'Could not accept the names');
		} finally {
			busy = null;
		}
	}

	async function readAgain(): Promise<void> {
		busy = 'preview';
		error = null;
		try {
			view = await requestDiscMenuPreview(jobId);
		} catch (e) {
			error = message(e, 'Could not read the menu again');
		} finally {
			busy = null;
		}
	}

	async function nameByHand(): Promise<void> {
		busy = 'hand';
		error = null;
		try {
			const created = await createMenuProposal(jobId, {
				track_id: handDraft.trackId,
				role: handDraft.role as TrackRole,
				name: handDraft.name.trim(),
				episode_number: handDraft.episode
			});
			replace(created);
			byHand = false;
			handDraft = { trackId: '', role: null, name: '', episode: null };
			onrenamed?.();
		} catch (e) {
			error = message(e, 'Could not rename that title');
		} finally {
			busy = null;
		}
	}

	function clickButton(b: MenuPreviewButtonView): void {
		if (b.outcome === 'page' && b.goes_to) pageId = b.goes_to;
		else if (b.proposal_id) focused = b.proposal_id;
	}

	const handBlocker = $derived(
		!handDraft.trackId
			? 'Pick a title'
			: !handDraft.role
				? 'Pick a role'
				: !handDraft.name.trim()
					? 'Give it a name'
					: handDraft.role === 'special' && !handDraft.episode
						? 'A special needs its season 0 episode number'
						: null
	);
</script>

{#if !absent && (view || error)}
	<section class="disc-menu-panel" data-testid="disc-menu-panel">
		<div class="disc-menu-head">
			<h2 class="disc-menu-title">Disc menu</h2>
			<span class="field-help" data-testid="dm-state">{previewState(view?.preview)}</span>
			{#if canEdit && view?.can_preview}
				<button
					type="button"
					class="btn btn-sm"
					data-testid="dm-read-again"
					disabled={busy !== null}
					onclick={readAgain}
				>
					<Glyph name="refresh" />
					{view?.preview ? 'Read the menu again' : 'Read the menu'}
				</button>
			{/if}
		</div>
		{#if error}
			<p class="field-error" data-testid="dm-error">{error}</p>
		{/if}

		{#if page}
			{#if pages.length > 1}
				<div class="tabs disc-menu-tabs" data-testid="dm-pages">
					{#each pages as p (p.id)}
						<button
							type="button"
							class="tabs-tab"
							data-selected={p.id === page.id}
							data-testid="dm-page-tab"
							onclick={() => (pageId = p.id)}>Page {p.id}</button
						>
					{/each}
				</div>
			{/if}
			<div class="disc-menu-stage" data-testid="dm-stage">
				{#if images[page.id]}
					<img class="disc-menu-image" src={images[page.id]} alt="Menu page {page.id}" />
				{:else}
					<div class="disc-menu-image disc-menu-image-missing"></div>
				{/if}
				{#each page.buttons as b (b.id)}
					{@const at = boxPercent(b.box, page.size)}
					{#if at}
						<button
							type="button"
							class="disc-menu-box"
							style:--x={at.x}
							style:--y={at.y}
							style:--w={at.w}
							style:--h={at.h}
							data-outcome={b.outcome}
							data-visible={b.visible}
							data-selected={b.proposal_id !== null && b.proposal_id === focused}
							data-testid="dm-box"
							title={buttonHint(b, pageName)}
							aria-label={buttonHint(b, pageName)}
							onclick={() => clickButton(b)}
						></button>
					{/if}
				{/each}
			</div>
			<p class="field-help">
				Each box is a menu button. Click one that opens another page to go there, or one that plays a title to find its
				name below.
			</p>
			{#if (view?.preview?.unsupported ?? []).length > 0 || view?.preview?.truncated}
				<p class="field-help" data-testid="dm-unsupported">
					{#if view?.preview?.truncated}The menu has more pages than the preview reads.{/if}
					{#if (view?.preview?.unsupported ?? []).length > 0}Not shown: {view?.preview?.unsupported?.join(', ')}.{/if}
				</p>
			{/if}
		{/if}

		{#if named.length > 0}
			<div class="disc-menu-proposals-head">
				<h3 class="disc-menu-subtitle">Names from the menu</h3>
				{#if canEdit && acceptable.length > 0}
					<button
						type="button"
						class="btn btn-primary btn-sm"
						data-testid="dm-accept-all"
						disabled={busy !== null}
						onclick={acceptAll}
					>
						<Glyph name="check" />
						Accept all ({acceptable.length})
					</button>
				{/if}
			</div>
			{#if refusedCount > 0}
				<p class="field-error" data-testid="dm-refused">
					{refusedCount}
					{refusedCount === 1 ? 'name was' : 'names were'} not accepted; see below.
				</p>
			{/if}
			<div class="overflow-x-auto">
				<table class="table responsive-table">
					<thead>
						<tr>
							<th class="table-header">Button</th>
							<th class="table-header">Title</th>
							<th class="table-header">Role</th>
							<th class="table-header">Name</th>
							<th class="table-header">Status</th>
							<th class="table-header"></th>
						</tr>
					</thead>
					<tbody>
						{#each named as p (p.id)}
							{@const d = draft(p)}
							{@const editable = canEdit && isOpen(p)}
							{@const blocker = acceptBlocker(p, d)}
							<tr class="table-row" data-selected={p.id === focused} data-testid="dm-proposal">
								<td class="table-cell" data-label="Button">
									<span class="disc-menu-label">{p.label || 'No label'}</span>
									<span class="disc-menu-faint">{p.plays}</span>
								</td>
								<td class="table-cell" data-label="Title">{trackName(p.track_id)}</td>
								<td class="table-cell" data-label="Role">
									{#if editable}
										<select
											class="field-control"
											aria-label="Role"
											data-testid="dm-role"
											value={d.role ?? ''}
											onchange={(e) => edit(p, { role: (e.currentTarget.value || null) as TrackRole | null })}
										>
											<option value="">Pick one</option>
											{#each MENU_ROLES as r (r.value)}
												<option value={r.value}>{r.label}</option>
											{/each}
										</select>
									{:else}
										{MENU_ROLES.find((r) => r.value === p.role)?.label ?? 'Not set'}
									{/if}
								</td>
								<td class="table-cell" data-label="Name">
									{#if editable}
										<div class="disc-menu-name">
											<input
												class="field-control"
												aria-label="Name"
												data-testid="dm-name"
												value={d.name}
												oninput={(e) => edit(p, { name: e.currentTarget.value })}
											/>
											{#if d.role === 'special'}
												<input
													class="field-control disc-menu-episode"
													type="number"
													min="1"
													aria-label="Season 0 episode"
													placeholder="Ep."
													data-testid="dm-episode"
													value={d.episode ?? ''}
													oninput={(e) => edit(p, { episode: e.currentTarget.valueAsNumber || null })}
												/>
											{/if}
										</div>
									{:else}
										{p.name ?? ''}
										{#if p.applied_path}
											<span class="disc-menu-faint mono" data-testid="dm-applied">{p.applied_path}</span>
										{/if}
									{/if}
									{#if rowErrors[p.id] || p.last_error}
										<span class="field-error" data-testid="dm-row-error">{rowErrors[p.id] ?? p.last_error}</span>
									{/if}
								</td>
								<td class="table-cell" data-label="Status">
									<span
										class="chip chip-sm {PROPOSAL_STATUS[p.status].tone
											? `chip-${PROPOSAL_STATUS[p.status].tone}`
											: ''}">{PROPOSAL_STATUS[p.status].text}</span
									>
								</td>
								<td class="table-cell" data-label="">
									{#if editable}
										<div class="cluster disc-menu-actions">
											<button
												type="button"
												class="btn btn-primary btn-sm"
												data-testid="dm-accept"
												disabled={busy !== null || blocker !== null}
												title={blocker ?? 'Rename the encode to this name'}
												onclick={() => act(p, 'accept')}>Accept</button
											>
											{#if p.status === 'rejected'}
												<button
													type="button"
													class="btn btn-sm"
													data-testid="dm-reopen"
													disabled={busy !== null}
													onclick={() => act(p, 'reopen')}>Reopen</button
												>
											{:else}
												<button
													type="button"
													class="btn btn-sm"
													data-testid="dm-reject"
													disabled={busy !== null}
													onclick={() => act(p, 'reject')}>Reject</button
												>
											{/if}
										</div>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}

		{#if notes.length > 0}
			<h3 class="disc-menu-subtitle">Also on the menu</h3>
			<ul class="disc-menu-notes" data-testid="dm-notes">
				{#each notes as n (n.id)}
					<li>
						<span class="disc-menu-label">{n.label || 'No label'}</span>
						<span class="disc-menu-faint">{n.plays}{n.note ? `: ${n.note}` : ''}</span>
					</li>
				{/each}
			</ul>
		{/if}

		{#if canEdit && titles.length > 0}
			{#if byHand}
				<div class="disc-menu-by-hand" data-testid="dm-by-hand">
					<select class="field-control" aria-label="Title" data-testid="dm-hand-track" bind:value={handDraft.trackId}>
						<option value="">Pick a title</option>
						{#each titles as t (t.id)}
							<option value={t.id}>Title {t.index}{t.title ? `: ${t.title}` : ''}</option>
						{/each}
					</select>
					<select class="field-control" aria-label="Role" data-testid="dm-hand-role" bind:value={handDraft.role}>
						<option value={null}>Pick a role</option>
						{#each MENU_ROLES as r (r.value)}
							<option value={r.value}>{r.label}</option>
						{/each}
					</select>
					<input class="field-control" aria-label="Name" data-testid="dm-hand-name" bind:value={handDraft.name} />
					{#if handDraft.role === 'special'}
						<input
							class="field-control disc-menu-episode"
							type="number"
							min="1"
							aria-label="Season 0 episode"
							placeholder="Ep."
							data-testid="dm-hand-episode"
							bind:value={handDraft.episode}
						/>
					{/if}
					<button
						type="button"
						class="btn btn-primary btn-sm"
						data-testid="dm-hand-save"
						disabled={busy !== null || handBlocker !== null}
						title={handBlocker ?? 'Rename the encode to this name'}
						onclick={nameByHand}>Rename</button
					>
					<button type="button" class="btn btn-sm" onclick={() => (byHand = false)}>Cancel</button>
				</div>
			{:else}
				<button type="button" class="btn btn-link" data-testid="dm-hand-open" onclick={() => (byHand = true)}>
					Name a title by hand
				</button>
			{/if}
		{/if}
	</section>
{/if}

<style>
	.disc-menu-panel {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	.disc-menu-head,
	.disc-menu-proposals-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 0.75rem;
	}
	.disc-menu-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.disc-menu-subtitle {
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.disc-menu-stage {
		position: relative;
		max-width: 960px;
		border: 1px solid var(--color-border);
	}
	.disc-menu-image {
		display: block;
		width: 100%;
		height: auto;
	}
	.disc-menu-image-missing {
		aspect-ratio: 16 / 9;
		background: var(--color-surface-raised);
	}
	.disc-menu-box {
		position: absolute;
		left: var(--x);
		top: var(--y);
		width: var(--w);
		height: var(--h);
		padding: 0;
		border: 2px solid var(--color-primary);
		background: transparent;
		cursor: pointer;
	}
	.disc-menu-box[data-outcome='page'] {
		border-style: dashed;
		border-color: var(--color-info);
	}
	.disc-menu-box[data-outcome='stays'],
	.disc-menu-box[data-outcome='stopped'] {
		border-color: var(--color-border-strong);
	}
	.disc-menu-box[data-visible='false'] {
		border-style: dotted;
	}
	.disc-menu-box:hover,
	.disc-menu-box:focus-visible,
	.disc-menu-box[data-selected='true'] {
		background: color-mix(in srgb, var(--color-primary) 25%, transparent);
	}
	.disc-menu-label {
		display: block;
		color: var(--color-text);
	}
	.disc-menu-faint {
		display: block;
		font-size: 0.8rem;
		color: var(--color-text-faint);
	}
	.disc-menu-name {
		display: flex;
		gap: 0.375rem;
	}
	.disc-menu-episode {
		width: 5rem;
	}
	.disc-menu-actions {
		gap: 0.375rem;
		flex-wrap: nowrap;
	}
	.disc-menu-notes {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}
	.disc-menu-by-hand {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
	}
	tr[data-selected='true'] {
		background: color-mix(in srgb, var(--color-primary) 10%, transparent);
	}
</style>
