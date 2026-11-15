import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import type { MenuProposalView } from '$lib/types/api.gen';
import MenuProposalEditor from './MenuProposalEditor.svelte';

vi.mock('$lib/api/discMenus', () => ({ updateMenuProposal: vi.fn() }));

import { updateMenuProposal } from '$lib/api/discMenus';
import { ApiError } from '$lib/api/client';

const updateMock = vi.mocked(updateMenuProposal);

const proposal = (over: Partial<MenuProposalView> = {}): MenuProposalView => ({
	id: 'mnp_promo',
	job_id: 'job_1',
	track_id: 'trk_8',
	page: '2',
	button: 12,
	label: 'Promo Videos',
	score: 0.99,
	plays: '00023.mpls',
	role: 'trailer',
	name: 'Promo Videos',
	episode_number: null,
	note: null,
	status: 'proposed',
	applied_path: null,
	last_error: null,
	created_at: '2026-11-12T00:00:00Z',
	updated_at: '2026-11-12T00:00:00Z',
	...over
});

function render(props: Record<string, unknown> = {}) {
	const onchange = vi.fn();
	renderComponent(MenuProposalEditor, {
		props: { jobId: 'job_1', proposal: proposal(), canEdit: true, onchange, ...props }
	});
	return onchange;
}

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

describe('MenuProposalEditor', () => {
	it('shows the button, the name and its status', () => {
		render();
		const row = screen.getByTestId('dm-proposal');
		expect(row).toHaveTextContent('Menu');
		expect(row).toHaveTextContent('Promo Videos');
		expect(row).toHaveTextContent('00023.mpls');
		expect(screen.getByTestId('dm-status')).toHaveTextContent('Proposed');
		expect(screen.getByTestId('dm-name')).toHaveValue('Promo Videos');
	});

	it('accepts an edited name and hands the saved one back', async () => {
		const saved = proposal({
			name: 'PV Collection',
			status: 'applied',
			applied_path: 'Show/trailers/PV Collection.mkv'
		});
		updateMock.mockResolvedValue(saved);
		const onchange = render();
		await fireEvent.input(screen.getByTestId('dm-name'), { target: { value: 'PV Collection ' } });
		await fireEvent.click(screen.getByTestId('dm-accept'));
		await waitFor(() =>
			expect(updateMock).toHaveBeenCalledWith('job_1', 'mnp_promo', {
				role: 'trailer',
				name: 'PV Collection',
				episode_number: null,
				action: 'accept'
			})
		);
		expect(onchange).toHaveBeenCalledWith(saved);
	});

	it('needs an episode number for a special', async () => {
		render();
		await fireEvent.change(screen.getByTestId('dm-role'), { target: { value: 'special' } });
		expect(screen.getByTestId('dm-accept')).toBeDisabled();
		expect(screen.getByTestId('dm-accept')).toHaveAttribute('title', 'A special needs its season 0 episode number');
		await fireEvent.input(screen.getByTestId('dm-episode'), { target: { value: '3' } });
		expect(screen.getByTestId('dm-accept')).not.toBeDisabled();
		await fireEvent.change(screen.getByTestId('dm-role'), { target: { value: '' } });
		expect(screen.getByTestId('dm-accept')).toHaveAttribute('title', 'Pick a role');
	});

	it('rejects, reopens, and shows a refusal', async () => {
		updateMock.mockResolvedValueOnce(proposal({ status: 'rejected' }));
		const onchange = render();
		await fireEvent.click(screen.getByTestId('dm-reject'));
		await waitFor(() => expect(updateMock).toHaveBeenCalledWith('job_1', 'mnp_promo', { action: 'reject' }));
		expect(onchange).toHaveBeenCalledWith(proposal({ status: 'rejected' }));
		cleanup();

		render({ proposal: proposal({ status: 'rejected' }) });
		updateMock.mockResolvedValueOnce(proposal());
		await fireEvent.click(screen.getByTestId('dm-reopen'));
		await waitFor(() => expect(updateMock).toHaveBeenLastCalledWith('job_1', 'mnp_promo', { action: 'reopen' }));
		cleanup();

		const quiet = render();
		updateMock.mockRejectedValueOnce(new ApiError(409, 'Show/trailers/Promo Videos.mkv already exists', null));
		await fireEvent.click(screen.getByTestId('dm-accept'));
		expect(await screen.findByTestId('dm-row-error')).toHaveTextContent('already exists');
		expect(quiet).not.toHaveBeenCalled();
		cleanup();

		updateMock.mockRejectedValueOnce('boom');
		render();
		await fireEvent.click(screen.getByTestId('dm-accept'));
		expect(await screen.findByTestId('dm-row-error')).toHaveTextContent('Could not save that');
	});

	it('is read-only once decided or for a guest, and shows where it went', () => {
		render({ proposal: proposal({ status: 'applied', applied_path: 'Show/trailers/Promo Videos.mkv' }) });
		expect(screen.queryByTestId('dm-accept')).toBeNull();
		expect(screen.getByTestId('dm-applied')).toHaveTextContent('Show/trailers/Promo Videos.mkv');
		expect(screen.getByTestId('dm-proposal')).toHaveTextContent('Trailer');
		cleanup();

		render({ canEdit: false, proposal: proposal({ last_error: 'stale', label: '', role: null }) });
		expect(screen.queryByTestId('dm-role')).toBeNull();
		expect(screen.getByTestId('dm-proposal')).toHaveTextContent('No label');
		expect(screen.getByTestId('dm-proposal')).toHaveTextContent('Not set');
		expect(screen.getByTestId('dm-row-error')).toHaveTextContent('stale');
	});

	it('says an accepted name waits for the title to be ripped', () => {
		render({ proposal: proposal({ status: 'accepted' }), trackStatus: 'queued' });
		expect(screen.getByTestId('dm-status')).toHaveTextContent('Waiting for the rip');
	});
});
