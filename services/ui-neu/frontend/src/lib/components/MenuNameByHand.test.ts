import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import type { MenuProposalView } from '$lib/types/api.gen';
import MenuNameByHand from './MenuNameByHand.svelte';

vi.mock('$lib/api/discMenus', () => ({ createMenuProposal: vi.fn() }));

import { createMenuProposal } from '$lib/api/discMenus';

const created = {
	id: 'mnp_hand',
	job_id: 'job_1',
	track_id: 'trk_7',
	page: null,
	button: null,
	label: '',
	plays: 'set by hand',
	role: 'special',
	name: 'Panel',
	episode_number: 2,
	status: 'accepted'
} as unknown as MenuProposalView;

function render() {
	const props = { jobId: 'job_1', trackId: 'trk_7', onsaved: vi.fn(), oncancel: vi.fn() };
	renderComponent(MenuNameByHand, { props });
	return props;
}

describe('MenuNameByHand', () => {
	afterEach(() => {
		cleanup();
		vi.clearAllMocks();
	});

	it('names the title, saying what is missing until it can', async () => {
		vi.mocked(createMenuProposal).mockResolvedValue(created);
		const props = render();
		const save = screen.getByTestId('dm-hand-save');
		expect(save).toHaveAttribute('title', 'Pick a role');
		await fireEvent.change(screen.getByTestId('dm-hand-role'), { target: { value: 'special' } });
		expect(save).toHaveAttribute('title', 'Give it a name');
		await fireEvent.input(screen.getByTestId('dm-hand-name'), { target: { value: ' Panel' } });
		expect(save).toHaveAttribute('title', 'A special needs its season 0 episode number');
		await fireEvent.input(screen.getByTestId('dm-hand-episode'), { target: { value: '2' } });
		expect(save).toBeEnabled();
		await fireEvent.click(save);
		await waitFor(() => expect(props.onsaved).toHaveBeenCalledWith(created));
		expect(createMenuProposal).toHaveBeenCalledWith('job_1', {
			track_id: 'trk_7',
			role: 'special',
			name: 'Panel',
			episode_number: 2
		});
	});

	it('shows why it could not be saved, and cancels', async () => {
		vi.mocked(createMenuProposal).mockRejectedValue(new Error('the title has no finished encode to rename'));
		const props = render();
		await fireEvent.change(screen.getByTestId('dm-hand-role'), { target: { value: 'extra' } });
		await fireEvent.input(screen.getByTestId('dm-hand-name'), { target: { value: 'Outtakes' } });
		await fireEvent.click(screen.getByTestId('dm-hand-save'));
		expect(await screen.findByTestId('dm-hand-error')).toHaveTextContent('no finished encode');
		expect(props.onsaved).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByTestId('dm-hand-cancel'));
		expect(props.oncancel).toHaveBeenCalledOnce();
	});
});
