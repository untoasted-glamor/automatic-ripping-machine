<script lang="ts">
	// Name one title by hand (fork only): its role and name, as if its menu button
	// had proposed them. Opened from the Episode cell of a title the menu doesn't
	// name, in the job page's Tracks table. A title not ripped yet takes the name
	// now and its file is laid out under it; a finished encode is renamed.
	import { createMenuProposal } from '$lib/api/discMenus';
	import type { MenuProposalView, TrackRole } from '$lib/types/api.gen';
	import { MENU_ROLES, draftBlocker } from '$lib/utils/disc-menu';

	interface Props {
		jobId: string;
		trackId: string;
		/** Called with the saved name (the page shows it as the title's menu name from then on). */
		onsaved: (created: MenuProposalView) => void;
		oncancel: () => void;
	}

	let { jobId, trackId, onsaved, oncancel }: Props = $props();

	let role = $state<TrackRole | null>(null);
	let name = $state('');
	let episode = $state<number | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);

	const blocker = $derived(draftBlocker({ role, name, episode }));

	async function save(): Promise<void> {
		busy = true;
		error = null;
		try {
			onsaved(
				await createMenuProposal(jobId, {
					track_id: trackId,
					role: role as TrackRole,
					name: name.trim(),
					episode_number: episode
				})
			);
		} catch (e) {
			error = e instanceof Error && e.message ? e.message : 'Could not name that title';
		} finally {
			busy = false;
		}
	}
</script>

<div class="menu-by-hand" data-testid="dm-by-hand">
	<span class="chip chip-sm">By hand</span>
	<select class="field-control" aria-label="Role" data-testid="dm-hand-role" bind:value={role}>
		<option value={null}>Pick a role</option>
		{#each MENU_ROLES as r (r.value)}
			<option value={r.value}>{r.label}</option>
		{/each}
	</select>
	<input class="field-control menu-by-hand-name" aria-label="Name" data-testid="dm-hand-name" bind:value={name} />
	{#if role === 'special'}
		<input
			class="field-control menu-by-hand-episode"
			type="number"
			min="1"
			aria-label="Season 0 episode"
			placeholder="Ep."
			data-testid="dm-hand-episode"
			bind:value={episode}
		/>
	{/if}
	<button
		type="button"
		class="btn btn-primary btn-sm"
		data-testid="dm-hand-save"
		disabled={busy || blocker !== null}
		title={blocker ?? 'Give the title this name (its encode is renamed if it has one)'}
		onclick={save}>{busy ? 'Saving...' : 'Save name'}</button
	>
	<button type="button" class="btn btn-sm" data-testid="dm-hand-cancel" onclick={oncancel}>Cancel</button>
	{#if error}
		<p class="field-error menu-by-hand-error" role="alert" data-testid="dm-hand-error">{error}</p>
	{/if}
</div>

<style>
	.menu-by-hand {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
	}
	.menu-by-hand-name {
		flex: 1 1 14rem;
		min-width: 0;
	}
	.menu-by-hand-episode {
		width: 5rem;
	}
	.menu-by-hand-error {
		flex-basis: 100%;
	}
</style>
