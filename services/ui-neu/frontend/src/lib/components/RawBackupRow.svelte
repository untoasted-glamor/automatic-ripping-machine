<script lang="ts">
	import { deleteJobBackup } from '$lib/api/jobs';
	import { formatBytes } from '$lib/utils/format';
	import type { RawBackupView } from '$lib/types/api.gen';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import Glyph from './Glyph.svelte';

	interface Props {
		jobId: string;
		backup: RawBackupView;
		// The ripper is using the backup, and the Backend refuses the delete.
		ripping: boolean;
		canDelete: boolean;
		ondeleted: () => void;
	}

	let { jobId, backup, ripping, canDelete, ondeleted }: Props = $props();

	// The ripper keeps this copy only while a kept title still needs it.
	const INFO =
		'A decrypted copy of the disc (an .iso for DVD, a folder for Blu-ray) that the kept titles are ' +
		'extracted from before transcoding. It is deleted automatically once every kept title is ' +
		'extracted; until then, failed titles can be retried from it without the disc.';

	let confirming = $state(false);
	let deleting = $state(false);
	let error = $state<string | null>(null);

	async function remove() {
		confirming = false;
		deleting = true;
		error = null;
		try {
			await deleteJobBackup(jobId);
			ondeleted();
		} catch (e) {
			error = `Could not delete the raw backup: ${e instanceof Error ? e.message : 'unknown error'}`;
		} finally {
			deleting = false;
		}
	}
</script>

<div class="raw-backup-row" data-testid="raw-backup">
	<span class="raw-backup-label">Raw backup</span>
	<span data-testid="raw-backup-size">{formatBytes(backup.size_bytes)}</span>
	{#if !backup.complete}
		<span class="raw-backup-faint">(incomplete)</span>
	{/if}
	<span class="raw-backup-info" title={INFO} data-testid="raw-backup-info">
		<Glyph name="info" class="h-3.5 w-3.5" label="About the raw backup" />
	</span>
	{#if canDelete && !ripping}
		<button
			type="button"
			class="btn btn-danger raw-backup-delete"
			disabled={deleting}
			onclick={() => (confirming = true)}
		>
			{deleting ? 'Deleting...' : 'Delete raw backup'}
		</button>
	{/if}
</div>
{#if error}
	<p class="alert alert-danger mt-2" role="alert" data-testid="raw-backup-error">{error}</p>
{/if}

<ConfirmDialog
	open={confirming}
	title="Delete the raw backup?"
	message={`The disc backup in /raw/${jobId}/.backup/ (${formatBytes(backup.size_bytes)}) will be deleted. The ripped files and the job stay. Titles that failed to rip can then only be retried by reading the disc again.`}
	confirmLabel="Delete raw backup"
	variant="danger"
	onconfirm={remove}
	oncancel={() => (confirming = false)}
/>

<style>
	.raw-backup-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.75rem;
		font-size: 0.875rem;
	}
	.raw-backup-label,
	.raw-backup-faint,
	.raw-backup-info {
		color: var(--color-text-faint);
	}
	.raw-backup-info {
		display: inline-flex;
		cursor: help;
	}
	.raw-backup-delete {
		padding: 0.25rem 0.625rem;
		font-size: 0.75rem;
	}
</style>
