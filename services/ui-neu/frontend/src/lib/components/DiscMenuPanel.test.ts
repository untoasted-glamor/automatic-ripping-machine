import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor, within } from '$lib/test-utils';
import { createTrack } from './__fixtures__/job';
import type { JobDiscMenuView, MenuProposalView } from '$lib/types/api.gen';
import DiscMenuPanel from './DiscMenuPanel.svelte';

vi.mock('$lib/api/discMenus', () => ({
	fetchDiscMenu: vi.fn(),
	fetchDiscMenuImage: vi.fn(),
	requestDiscMenuPreview: vi.fn(),
	updateMenuProposal: vi.fn(),
	createMenuProposal: vi.fn(),
	buildDiscMenuPack: vi.fn(),
	deleteDiscMenuPack: vi.fn()
}));

import {
	buildDiscMenuPack,
	createMenuProposal,
	deleteDiscMenuPack,
	fetchDiscMenu,
	fetchDiscMenuImage,
	requestDiscMenuPreview
} from '$lib/api/discMenus';
import { ApiError } from '$lib/api/client';

const fetchMock = vi.mocked(fetchDiscMenu);

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

function menu(over: Partial<JobDiscMenuView> = {}): JobDiscMenuView {
	return {
		track_id: 'trk_menu',
		track_status: 'done',
		size_bytes: 100,
		can_preview: true,
		preview: {
			task_id: 'txt_1',
			status: 'done',
			progress_pct: 100,
			disc: 'bd',
			notes: [],
			unsupported: ['button sounds'],
			truncated: false,
			pages: [
				{
					id: '1',
					at: 'm p2',
					language: 'eng',
					image: 'pages/1.jpg',
					size: [1920, 1080],
					buttons: [
						{ id: 13, box: [0, 0, 100, 50], visible: true, label: 'Special Features', outcome: 'page', goes_to: '2' },
						{ id: 14, box: [0, 0, 0, 0], visible: false, outcome: 'stays' }
					]
				},
				{
					id: '2',
					at: 'm p8',
					language: 'eng',
					image: 'pages/2.jpg',
					size: [1920, 1080],
					buttons: [
						{
							id: 12,
							box: [10, 10, 200, 60],
							visible: true,
							label: 'Promo Videos',
							outcome: 'plays',
							plays: '00023.mpls',
							track_id: 'trk_8',
							proposal_id: 'mnp_promo'
						}
					]
				}
			]
		},
		proposals: [
			proposal(),
			proposal({
				id: 'mnp_op',
				track_id: null,
				label: 'Textless OP',
				plays: '00024.mpls',
				role: 'other',
				note: 'not ripped',
				status: 'info'
			})
		],
		...over
	};
}

const tracks = [
	createTrack({ id: 'trk_menu', kind: 'disc_menu', index: -1, status: 'done' }),
	createTrack({ id: 'trk_7', kind: 'video_title', index: 7, status: 'done' }),
	createTrack({ id: 'trk_8', kind: 'video_title', index: 8, status: 'done' })
];

function render(props: Record<string, unknown> = {}) {
	const onrenamed = vi.fn();
	renderComponent(DiscMenuPanel, {
		props: { jobId: 'job_1', tracks, canEdit: true, onrenamed, onproposals, onfocus, ...props }
	});
	return onrenamed;
}

const onproposals = vi.fn();
const onfocus = vi.fn();

beforeEach(() => {
	vi.mocked(fetchDiscMenuImage).mockImplementation(async (_job, rel) => `blob:${rel}`);
});

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	vi.useRealTimers();
});

describe('DiscMenuPanel', () => {
	it('renders nothing for a job with no disc menu', async () => {
		fetchMock.mockRejectedValue(new ApiError(404, 'no disc menu', null));
		render();
		await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('job_1'));
		expect(screen.queryByTestId('disc-menu-panel')).toBeNull();
	});

	it('shows a load error', async () => {
		fetchMock.mockRejectedValue(new Error('boom'));
		render();
		expect(await screen.findByTestId('dm-error')).toHaveTextContent('boom');
	});

	it('shows the pages with their buttons, and follows a button to its page', async () => {
		fetchMock.mockResolvedValue(menu());
		render();
		expect(await screen.findByTestId('dm-state')).toHaveTextContent('2 pages read from the menu.');
		expect(await screen.findByAltText('Menu page 1')).toHaveAttribute('src', 'blob:pages/1.jpg');
		// The hidden-logic button has no box to draw.
		const boxes = screen.getAllByTestId('dm-box');
		expect(boxes).toHaveLength(1);
		expect(boxes[0]).toHaveAttribute('title', '"Special Features": opens page 2');
		expect(screen.getByTestId('dm-unsupported')).toHaveTextContent('Not shown: button sounds.');
		await fireEvent.click(boxes[0]);
		expect(await screen.findByAltText('Menu page 2')).toHaveAttribute('src', 'blob:pages/2.jpg');
		const play = screen.getByTestId('dm-box');
		expect(play).toHaveAttribute('title', '"Promo Videos": plays 00023.mpls');
		await fireEvent.click(play);
		expect(play).toHaveAttribute('data-selected', 'true');
		expect(onfocus).toHaveBeenCalledWith('mnp_promo');
		const tabs = screen.getAllByTestId('dm-page-tab');
		await fireEvent.click(tabs[0]);
		expect(tabs[0]).toHaveAttribute('data-selected', 'true');
	});

	it('lists the menu notes and hands its proposals to the page', async () => {
		fetchMock.mockResolvedValue(menu());
		render();
		expect(await screen.findByTestId('dm-notes')).toHaveTextContent('Textless OP 00024.mpls: not ripped');
		await waitFor(() => expect(onproposals).toHaveBeenCalledWith(menu().proposals));
		// The names themselves are on the Tracks table's rows, not here.
		expect(screen.queryByTestId('dm-proposal')).toBeNull();
		expect(screen.queryByTestId('dm-accept-all')).toBeNull();
	});

	it('reads the menu again and polls until it is done', async () => {
		vi.useFakeTimers();
		fetchMock.mockResolvedValue(menu({ preview: null, proposals: [] }));
		vi.mocked(requestDiscMenuPreview).mockResolvedValue(
			menu({ can_preview: false, preview: { task_id: 'txt_2', status: 'queued', progress_pct: 0 }, proposals: [] })
		);
		render();
		const again = await screen.findByTestId('dm-read-again');
		expect(again).toHaveTextContent('Read the menu');
		expect(screen.getByTestId('dm-state')).toHaveTextContent('The menu has not been read yet.');
		await fireEvent.click(again);
		expect(await screen.findByText('Waiting to read the menu.')).toBeInTheDocument();
		expect(screen.queryByTestId('dm-read-again')).toBeNull();
		fetchMock.mockResolvedValue(menu());
		await vi.advanceTimersByTimeAsync(4100);
		await waitFor(() => expect(screen.getByTestId('dm-state')).toHaveTextContent('2 pages read'));
		const calls = fetchMock.mock.calls.length;
		await vi.advanceTimersByTimeAsync(8200);
		expect(fetchMock.mock.calls.length).toBe(calls);
	});

	it('shows a read-again failure', async () => {
		fetchMock.mockResolvedValue(menu());
		vi.mocked(requestDiscMenuPreview).mockRejectedValue(new ApiError(409, 'already queued', null));
		render();
		await fireEvent.click(await screen.findByTestId('dm-read-again'));
		expect(await screen.findByTestId('dm-error')).toHaveTextContent('already queued');
	});

	it('names a title by hand', async () => {
		fetchMock.mockResolvedValue(menu());
		vi.mocked(createMenuProposal).mockResolvedValue(
			proposal({
				id: 'mnp_hand',
				track_id: 'trk_7',
				label: '',
				plays: 'set by hand',
				role: 'special',
				name: 'Panel',
				episode_number: 2,
				status: 'applied'
			})
		);
		const onrenamed = render();
		await fireEvent.click(await screen.findByTestId('dm-hand-open'));
		const save = screen.getByTestId('dm-hand-save');
		expect(save).toHaveAttribute('title', 'Pick a title');
		await fireEvent.change(screen.getByTestId('dm-hand-track'), { target: { value: 'trk_7' } });
		expect(save).toHaveAttribute('title', 'Pick a role');
		await fireEvent.change(screen.getByTestId('dm-hand-role'), { target: { value: 'special' } });
		expect(save).toHaveAttribute('title', 'Give it a name');
		await fireEvent.input(screen.getByTestId('dm-hand-name'), { target: { value: ' Panel' } });
		expect(save).toHaveAttribute('title', 'A special needs its season 0 episode number');
		await fireEvent.input(screen.getByTestId('dm-hand-episode'), { target: { value: '2' } });
		expect(save).not.toBeDisabled();
		await fireEvent.click(save);
		await waitFor(() =>
			expect(createMenuProposal).toHaveBeenCalledWith('job_1', {
				track_id: 'trk_7',
				role: 'special',
				name: 'Panel',
				episode_number: 2
			})
		);
		expect(onrenamed).toHaveBeenCalled();
		expect(screen.queryByTestId('dm-by-hand')).toBeNull();
		await waitFor(() =>
			expect(onproposals).toHaveBeenLastCalledWith(
				expect.arrayContaining([expect.objectContaining({ id: 'mnp_hand' })])
			)
		);
	});

	it('shows a by-hand failure', async () => {
		fetchMock.mockResolvedValue(menu());
		vi.mocked(createMenuProposal).mockRejectedValue(new Error('no encode'));
		render();
		await fireEvent.click(await screen.findByTestId('dm-hand-open'));
		await fireEvent.change(screen.getByTestId('dm-hand-track'), { target: { value: 'trk_7' } });
		await fireEvent.change(screen.getByTestId('dm-hand-role'), { target: { value: 'extra' } });
		await fireEvent.input(screen.getByTestId('dm-hand-name'), { target: { value: 'Panel' } });
		await fireEvent.click(screen.getByTestId('dm-hand-save'));
		expect(await screen.findByTestId('dm-error')).toHaveTextContent('no encode');
	});

	it('is read-only for a guest', async () => {
		fetchMock.mockResolvedValue(menu());
		render({ canEdit: false });
		expect(await screen.findByTestId('dm-notes')).toBeInTheDocument();
		expect(screen.queryByTestId('dm-read-again')).toBeNull();
		expect(screen.queryByTestId('dm-hand-open')).toBeNull();
	});

	it('builds the menu pack and polls until it is built', async () => {
		vi.useFakeTimers();
		fetchMock.mockResolvedValue(menu());
		vi.mocked(buildDiscMenuPack).mockResolvedValue(
			menu({
				pack: { task_id: 'txt_p', status: 'queued', progress_pct: 0 },
				pack_refusal: 'a pack build is already queued or running'
			})
		);
		render();
		expect(await screen.findByTestId('dm-pack-state')).toHaveTextContent('No menu pack yet');
		const build = screen.getByTestId('dm-pack-build');
		expect(build).toHaveTextContent('Build');
		await fireEvent.click(build);
		expect(await screen.findByText('Waiting to build the menu pack.')).toBeInTheDocument();
		expect(screen.queryByTestId('dm-pack-build')).toBeNull();
		expect(screen.queryByTestId('dm-pack-refusal')).toBeNull();
		fetchMock.mockResolvedValue(
			menu({
				pack: { task_id: 'txt_p', status: 'done', progress_pct: 100, path: 'Show/.discmenus/bd-d2', size_bytes: 2048 }
			})
		);
		await vi.advanceTimersByTimeAsync(4100);
		await waitFor(() => expect(screen.getByTestId('dm-pack-state')).toHaveTextContent('in the show folder'));
		expect(screen.getByTestId('dm-pack-path')).toHaveTextContent('Show/.discmenus/bd-d2');
		expect(screen.getByTestId('dm-pack')).toHaveTextContent('2 KB');
		expect(screen.getByTestId('dm-pack-build')).toHaveTextContent('Build again');
	});

	it('says why the pack cannot be built, and shows a build failure', async () => {
		fetchMock.mockResolvedValue(menu({ pack_refusal: 'the job has no finished encodes yet' }));
		render();
		expect(await screen.findByTestId('dm-pack-refusal')).toHaveTextContent(
			"Can't build now: the job has no finished encodes yet"
		);
		expect(screen.queryByTestId('dm-pack-build')).toBeNull();
		cleanup();
		fetchMock.mockResolvedValue(menu());
		vi.mocked(buildDiscMenuPack).mockRejectedValue(new Error('a pack build is already queued or running'));
		render();
		await fireEvent.click(await screen.findByTestId('dm-pack-build'));
		expect(await screen.findByTestId('dm-error')).toHaveTextContent('already queued');
	});

	it('deletes the pack after asking', async () => {
		const built = { task_id: 'txt_p', status: 'done' as const, progress_pct: 100, path: 'Show/.discmenus/bd-d2' };
		fetchMock.mockResolvedValue(menu({ pack: built }));
		vi.mocked(deleteDiscMenuPack).mockResolvedValue(menu({ pack: { ...built, path: null } }));
		render();
		await fireEvent.click(await screen.findByTestId('dm-pack-delete'));
		expect(screen.getByText('Delete the menu pack?')).toBeInTheDocument();
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
		expect(deleteDiscMenuPack).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByTestId('dm-pack-delete'));
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete pack' }));
		await waitFor(() => expect(deleteDiscMenuPack).toHaveBeenCalledWith('job_1'));
		expect(await screen.findByTestId('dm-pack-state')).toHaveTextContent('not in the show folder any more');
		expect(screen.queryByTestId('dm-pack-delete')).toBeNull();
	});

	it('shows a delete failure', async () => {
		fetchMock.mockResolvedValue(
			menu({ pack: { task_id: 'txt_p', status: 'done', progress_pct: 100, path: 'Show/.discmenus/bd-d2' } })
		);
		vi.mocked(deleteDiscMenuPack).mockRejectedValue(new Error('read-only'));
		render();
		await fireEvent.click(await screen.findByTestId('dm-pack-delete'));
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete pack' }));
		expect(await screen.findByTestId('dm-error')).toHaveTextContent('read-only');
	});

	it('shows the pack but no pack buttons to a guest', async () => {
		fetchMock.mockResolvedValue(
			menu({ pack: { task_id: 'txt_p', status: 'done', progress_pct: 100, path: 'Show/.discmenus/bd-d2' } })
		);
		render({ canEdit: false });
		expect(await screen.findByTestId('dm-pack-path')).toBeInTheDocument();
		expect(screen.queryByTestId('dm-pack-build')).toBeNull();
		expect(screen.queryByTestId('dm-pack-delete')).toBeNull();
	});

	it('draws the page without its picture when the image fails', async () => {
		vi.mocked(fetchDiscMenuImage).mockRejectedValue(new Error('gone'));
		fetchMock.mockResolvedValue(menu());
		render();
		expect(await screen.findAllByTestId('dm-box')).toHaveLength(1);
		expect(screen.queryByAltText('Menu page 1')).toBeNull();
	});
});
