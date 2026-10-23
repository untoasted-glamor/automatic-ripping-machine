import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import RawBackupRow from './RawBackupRow.svelte';

vi.mock('$lib/api/jobs', () => ({
	deleteJobBackup: vi.fn(() => Promise.resolve())
}));

import { deleteJobBackup } from '$lib/api/jobs';
const mockDelete = vi.mocked(deleteJobBackup);

const GB = 1024 ** 3;

function render(props: Partial<{ ripping: boolean; canDelete: boolean; complete: boolean }> = {}) {
	const ondeleted = vi.fn();
	renderComponent(RawBackupRow, {
		props: {
			jobId: 'job_1',
			backup: { size_bytes: 46.5 * GB, complete: props.complete ?? true },
			ripping: props.ripping ?? false,
			canDelete: props.canDelete ?? true,
			ondeleted
		}
	});
	return ondeleted;
}

describe('RawBackupRow', () => {
	afterEach(() => {
		cleanup();
		vi.clearAllMocks();
	});

	it('shows the size and explains the backup', () => {
		render();
		expect(screen.getByTestId('raw-backup-size')).toHaveTextContent('46.5 GB');
		expect(screen.getByTestId('raw-backup-info').getAttribute('title')).toContain(
			'an .iso for DVD, a folder for Blu-ray'
		);
		expect(screen.queryByText('(incomplete)')).toBeNull();
	});

	it('marks a backup with no completion marker incomplete', () => {
		render({ complete: false });
		expect(screen.getByText('(incomplete)')).toBeInTheDocument();
	});

	it('deletes the backup only after the confirm', async () => {
		const ondeleted = render();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete raw backup' }));
		expect(mockDelete).not.toHaveBeenCalled();
		expect(screen.getByRole('dialog')).toHaveTextContent('/raw/job_1/.backup/ (46.5 GB)');

		const confirm = screen.getAllByRole('button', { name: 'Delete raw backup' }).at(-1)!;
		await fireEvent.click(confirm);

		expect(mockDelete).toHaveBeenCalledWith('job_1');
		await waitFor(() => expect(ondeleted).toHaveBeenCalled());
	});

	it('cancelling the confirm deletes nothing', async () => {
		render();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete raw backup' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(mockDelete).not.toHaveBeenCalled();
	});

	it('shows why a refused delete failed', async () => {
		mockDelete.mockRejectedValueOnce(new Error('job is ripping; the ripper is using its raw backup'));
		const ondeleted = render();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete raw backup' }));
		await fireEvent.click(screen.getAllByRole('button', { name: 'Delete raw backup' }).at(-1)!);
		await waitFor(() =>
			expect(screen.getByTestId('raw-backup-error')).toHaveTextContent(
				'Could not delete the raw backup: job is ripping; the ripper is using its raw backup'
			)
		);
		expect(ondeleted).not.toHaveBeenCalled();
	});

	it('offers no delete while ripping or to a reader', () => {
		render({ ripping: true });
		expect(screen.queryByRole('button', { name: 'Delete raw backup' })).toBeNull();
		cleanup();
		render({ canDelete: false });
		expect(screen.queryByRole('button', { name: 'Delete raw backup' })).toBeNull();
		expect(screen.getByTestId('raw-backup')).toBeInTheDocument();
	});
});
