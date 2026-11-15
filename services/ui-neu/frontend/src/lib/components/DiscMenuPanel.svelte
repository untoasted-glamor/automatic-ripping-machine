<script lang="ts">
	// Job page: the disc menu (fork only, disc menus M3-M4). Shows each menu
	// page the preview read, with every button's box over it, and the menu's
	// notes. The names the buttons propose are shown on the titles' rows in the
	// Tracks table (`MenuProposalEditor`): this panel loads them and hands them
	// to the page (`onproposals`), and a box that plays a title scrolls to its
	// row. A title the menu doesn't name is named by hand from its row's Episode
	// cell (`MenuNameByHand`). The menu
	// pack row says where the pack Jellyfin plays stands, and builds or deletes
	// it. Shown only when the job has a copied disc menu (the GET 404s otherwise).
	import { onDestroy } from 'svelte';
	import {
		buildDiscMenuPack,
		deleteDiscMenuPack,
		fetchDiscMenu,
		fetchDiscMenuImage,
		requestDiscMenuPreview
	} from '$lib/api/discMenus';
	import { ApiError } from '$lib/api/client';
	import type { JobDiscMenuView, MenuPreviewButtonView, MenuProposalView } from '$lib/types/api.gen';
	import { boxPercent, buttonHint, isPackLive, isPreviewLive, packState, previewState } from '$lib/utils/disc-menu';
	import { usePoll } from '$lib/utils/poll';
	import { formatBytes } from '$lib/utils/format';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import Glyph from './Glyph.svelte';

	interface Props {
		jobId: string;
		canEdit: boolean;
		/** Called with the menu's proposals whenever they load, for the Tracks table's rows. */
		onproposals?: (proposals: MenuProposalView[]) => void;
		/** A box that plays a title was clicked: the page brings that title's row (its track) into view. */
		onfocus?: (trackId: string) => void;
	}

	let { jobId, canEdit, onproposals, onfocus }: Props = $props();

	let view = $state<JobDiscMenuView | null>(null);
	let absent = $state(false);
	let error = $state<string | null>(null);
	let pageId = $state<string | null>(null);
	let focused = $state<string | null>(null);
	let busy = $state<string | null>(null);
	let images = $state<Record<string, string>>({});
	let confirmDelete = $state(false);

	const pages = $derived(view?.preview?.pages ?? []);
	const page = $derived(pages.find((p) => p.id === pageId) ?? pages[0] ?? null);
	const notes = $derived((view?.proposals ?? []).filter((p) => p.status === 'info'));
	function pageName(id: string): string {
		const p = pages.find((x) => x.id === id);
		return p ? `page ${p.id}` : `page ${id}`;
	}

	function message(e: unknown, fallback: string): string {
		return e instanceof Error && e.message ? e.message : fallback;
	}

	/** Load the panel again (the page calls this after a name changes in the Tracks table). */
	export async function reload(): Promise<void> {
		await load();
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

	$effect(() => {
		if (view) onproposals?.(view.proposals ?? []);
	});

	// Poll while the preview or the pack build runs; stop once both are done or failed.
	$effect(() => {
		if (!isPreviewLive(view?.preview) && !isPackLive(view?.pack)) return;
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

	async function buildPack(): Promise<void> {
		busy = 'pack';
		error = null;
		try {
			view = await buildDiscMenuPack(jobId);
		} catch (e) {
			error = message(e, 'Could not build the menu pack');
		} finally {
			busy = null;
		}
	}

	async function deletePack(): Promise<void> {
		confirmDelete = false;
		busy = 'pack';
		error = null;
		try {
			view = await deleteDiscMenuPack(jobId);
		} catch (e) {
			error = message(e, 'Could not delete the menu pack');
		} finally {
			busy = null;
		}
	}

	function clickButton(b: MenuPreviewButtonView): void {
		if (b.outcome === 'page' && b.goes_to) pageId = b.goes_to;
		else if (b.track_id) {
			focused = b.track_id;
			onfocus?.(b.track_id);
		}
	}
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

		<div class="disc-menu-pack" data-testid="dm-pack">
			<span class="field-help" data-testid="dm-pack-state">{packState(view?.pack)}</span>
			{#if view?.pack?.path}
				<span class="disc-menu-faint mono" data-testid="dm-pack-path">{view.pack.path}</span>
				{#if view.pack.size_bytes != null}
					<span class="disc-menu-faint">{formatBytes(view.pack.size_bytes)}</span>
				{/if}
			{/if}
			{#if canEdit}
				<span class="disc-menu-pack-actions">
					{#if view && !view.pack_refusal}
						<button
							type="button"
							class="btn btn-sm"
							data-testid="dm-pack-build"
							disabled={busy !== null}
							onclick={buildPack}
						>
							<Glyph name="refresh" />
							{view.pack ? 'Build again' : 'Build'}
						</button>
					{/if}
					{#if view?.pack?.path}
						<button
							type="button"
							class="btn btn-sm"
							data-testid="dm-pack-delete"
							disabled={busy !== null}
							onclick={() => (confirmDelete = true)}
						>
							<Glyph name="x" />
							Delete pack
						</button>
					{/if}
				</span>
			{/if}
			{#if canEdit && view?.pack_refusal && !isPackLive(view?.pack)}
				<span class="disc-menu-faint" data-testid="dm-pack-refusal">Can't build now: {view.pack_refusal}</span>
			{/if}
		</div>

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
							data-selected={b.track_id != null && b.track_id === focused}
							data-testid="dm-box"
							title={buttonHint(b, pageName)}
							aria-label={buttonHint(b, pageName)}
							onclick={() => clickButton(b)}
						>
							{#if b.outcome === 'plays' && b.title_index != null}
								<!-- Where the jump lands (the Tracks table's #), and how many clips its playlist plays. -->
								<span class="disc-menu-markers">
									<span class="disc-menu-marker" data-testid="dm-marker" data-untracked={b.track_id == null}
										>{b.title_index}</span
									>
									{#if (b.clip_titles ?? []).length > 1}
										<span class="disc-menu-marker disc-menu-marker-clips" data-testid="dm-marker-clips"
											>{b.clip_titles!.length} clips</span
										>
									{/if}
								</span>
							{/if}
						</button>
					{/if}
				{/each}
			</div>
			<p class="field-help">
				Each box is a menu button. Click one that opens another page to go there, or one that plays a title to go to its
				row in the Tracks table. A number is the title (the Tracks table's #) the button starts on; "clips" says its
				playlist plays several clips in a row (hover for their titles).
			</p>
			{#if (view?.preview?.unsupported ?? []).length > 0 || view?.preview?.truncated}
				<p class="field-help" data-testid="dm-unsupported">
					{#if view?.preview?.truncated}The menu has more pages than the preview reads.{/if}
					{#if (view?.preview?.unsupported ?? []).length > 0}Not shown: {view?.preview?.unsupported?.join(', ')}.{/if}
				</p>
			{/if}
		{/if}

		{#if notes.length > 0}
			<h3 class="disc-menu-subtitle">Also on the menu</h3>
			<ul class="disc-menu-notes" data-testid="dm-notes">
				{#each notes as n (n.id)}
					<li class="disc-menu-note" data-testid="dm-note">
						<div>
							<span class="disc-menu-label">{n.label || 'No label'}</span>
							<span class="disc-menu-faint">{n.plays}{n.note ? `: ${n.note}` : ''}</span>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
	<ConfirmDialog
		open={confirmDelete}
		title="Delete the menu pack?"
		message="The menu pack is removed from the show folder, so Jellyfin shows no menu for this disc. The copied menu stays, and Build makes the pack again."
		confirmLabel="Delete pack"
		variant="danger"
		onconfirm={() => void deletePack()}
		oncancel={() => (confirmDelete = false)}
	/>
{/if}

<style>
	.disc-menu-panel {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	.disc-menu-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 0.75rem;
	}
	.disc-menu-pack {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.25rem 0.75rem;
	}
	.disc-menu-pack .disc-menu-faint {
		display: inline;
	}
	.disc-menu-pack-actions {
		display: inline-flex;
		gap: 0.375rem;
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
	.disc-menu-markers {
		position: absolute;
		top: 50%;
		right: 0.25rem;
		display: flex;
		gap: 0.25rem;
		transform: translateY(-50%);
		pointer-events: none;
	}
	.disc-menu-marker {
		border-radius: 999px;
		background: var(--color-primary);
		padding: 0 0.4rem;
		font-size: 0.7rem;
		font-weight: 700;
		line-height: 1.15rem;
		color: var(--color-on-primary);
		white-space: nowrap;
	}
	.disc-menu-marker[data-untracked='true'] {
		border: 1px dashed currentColor;
		background: var(--color-surface-raised);
		color: var(--color-text);
	}
	.disc-menu-marker-clips {
		border: 1px solid var(--color-warning);
		background: var(--color-surface-raised);
		color: var(--color-text);
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
	.disc-menu-notes {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}
	.disc-menu-note {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
</style>
