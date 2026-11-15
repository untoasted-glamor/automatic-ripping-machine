<script lang="ts">
	// One name the disc menu proposes for a title (fork only), shown under the
	// title's row in the job page's Tracks table: the button it came from, its
	// role and name (editable while it is open), its status, and Accept /
	// Reject / Reopen. Accepting renames the finished encode into its bonus
	// folder, or names a title not encoded yet (fan-out then lays it out).
	import { updateMenuProposal } from '$lib/api/discMenus';
	import type { MenuProposalView, TrackRole, TrackStatus } from '$lib/types/api.gen';
	import { MENU_ROLES, PROPOSAL_STATUS, acceptBlocker, draftOf, isOpen, statusText } from '$lib/utils/disc-menu';

	interface Props {
		jobId: string;
		proposal: MenuProposalView;
		/** The status of the proposal's title: an accepted name waits for its rip. */
		trackStatus?: TrackStatus | null;
		canEdit: boolean;
		/** Called with the saved proposal. */
		onchange?: (updated: MenuProposalView) => void;
	}

	let { jobId, proposal, trackStatus = null, canEdit, onchange }: Props = $props();

	let edits = $state<Partial<ReturnType<typeof draftOf>>>({});
	let busy = $state(false);
	let error = $state<string | null>(null);

	const draft = $derived({ ...draftOf(proposal), ...edits });
	const editable = $derived(canEdit && isOpen(proposal));
	const blocker = $derived(acceptBlocker(proposal, draft));

	async function act(action: 'accept' | 'reject' | 'reopen'): Promise<void> {
		busy = true;
		error = null;
		try {
			const body =
				action === 'accept'
					? { role: draft.role, name: draft.name.trim(), episode_number: draft.episode, action }
					: { action };
			const updated = await updateMenuProposal(jobId, proposal.id, body);
			edits = {};
			onchange?.(updated);
		} catch (e) {
			error = e instanceof Error && e.message ? e.message : 'Could not save that';
		} finally {
			busy = false;
		}
	}
</script>

<div class="menu-proposal" data-testid="dm-proposal" data-proposal-id={proposal.id}>
	<span class="chip chip-sm" data-testid="dm-source">Menu</span>
	<span class="menu-proposal-button">
		<span class="menu-proposal-label">{proposal.label || 'No label'}</span>
		<span class="menu-proposal-faint">{proposal.plays}</span>
	</span>
	{#if editable}
		<select
			class="field-control menu-proposal-role"
			aria-label="Role"
			data-testid="dm-role"
			value={draft.role ?? ''}
			onchange={(e) => (edits = { ...edits, role: (e.currentTarget.value || null) as TrackRole | null })}
		>
			<option value="">Pick one</option>
			{#each MENU_ROLES as r (r.value)}
				<option value={r.value}>{r.label}</option>
			{/each}
		</select>
		<input
			class="field-control menu-proposal-name"
			aria-label="Name"
			data-testid="dm-name"
			value={draft.name}
			oninput={(e) => (edits = { ...edits, name: e.currentTarget.value })}
		/>
		{#if draft.role === 'special'}
			<input
				class="field-control menu-proposal-episode"
				type="number"
				min="1"
				aria-label="Season 0 episode"
				placeholder="Ep."
				data-testid="dm-episode"
				value={draft.episode ?? ''}
				oninput={(e) => (edits = { ...edits, episode: e.currentTarget.valueAsNumber || null })}
			/>
		{/if}
	{:else}
		<span>{MENU_ROLES.find((r) => r.value === proposal.role)?.label ?? 'Not set'}</span>
		<span>{proposal.name ?? ''}</span>
		{#if proposal.applied_path}
			<span class="menu-proposal-faint mono" data-testid="dm-applied">{proposal.applied_path}</span>
		{/if}
	{/if}
	<span
		class="chip chip-sm {PROPOSAL_STATUS[proposal.status].tone ? `chip-${PROPOSAL_STATUS[proposal.status].tone}` : ''}"
		data-testid="dm-status">{statusText(proposal, trackStatus)}</span
	>
	{#if editable}
		<span class="cluster menu-proposal-actions">
			<button
				type="button"
				class="btn btn-primary btn-sm"
				data-testid="dm-accept"
				disabled={busy || blocker !== null}
				title={blocker ?? 'Give the title this name'}
				onclick={() => act('accept')}>Accept</button
			>
			{#if proposal.status === 'rejected'}
				<button type="button" class="btn btn-sm" data-testid="dm-reopen" disabled={busy} onclick={() => act('reopen')}
					>Reopen</button
				>
			{:else}
				<button type="button" class="btn btn-sm" data-testid="dm-reject" disabled={busy} onclick={() => act('reject')}
					>Reject</button
				>
			{/if}
		</span>
	{/if}
	{#if error || proposal.last_error}
		<span class="field-error" data-testid="dm-row-error">{error ?? proposal.last_error}</span>
	{/if}
</div>

<style>
	.menu-proposal {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
	}
	.menu-proposal-button {
		display: inline-flex;
		flex-direction: column;
	}
	.menu-proposal-label {
		font-weight: 500;
	}
	.menu-proposal-faint {
		font-size: 0.75rem;
		color: var(--color-text-faint);
	}
	.menu-proposal-role {
		width: auto;
	}
	.menu-proposal-name {
		flex: 1 1 12rem;
		min-width: 8rem;
	}
	.menu-proposal-episode {
		width: 5rem;
	}
</style>
