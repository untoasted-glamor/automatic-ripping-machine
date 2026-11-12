import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import { createTrack } from './__fixtures__/job';
import type { JobDiscMenuView, MenuProposalView } from '$lib/types/api.gen';
import DiscMenuPanel from './DiscMenuPanel.svelte';

vi.mock('$lib/api/discMenus', () => ({
	fetchDiscMenu: vi.fn(),
	fetchDiscMenuImage: vi.fn(),
	requestDiscMenuPreview: vi.fn(),
	updateMenuProposal: vi.fn(),
	createMenuProposal: vi.fn(),
	acceptAllMenuProposals: vi.fn(),
	ripMenuTitle: vi.fn()
}));

import {
	acceptAllMenuProposals,
	createMenuProposal,
	fetchDiscMenu,
	fetchDiscMenuImage,
	requestDiscMenuPreview,
	ripMenuTitle,
	updateMenuProposal
} from '$lib/api/discMenus';
import { ApiError } from '$lib/api/client';

const fetchMock = vi.mocked(fetchDiscMenu);
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
	renderComponent(DiscMenuPanel, { props: { jobId: 'job_1', tracks, canEdit: true, onrenamed, ...props } });
	return onrenamed;
}

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
		expect(screen.getByTestId('dm-proposal')).toHaveAttribute('data-selected', 'true');
		const tabs = screen.getAllByTestId('dm-page-tab');
		await fireEvent.click(tabs[0]);
		expect(tabs[0]).toHaveAttribute('data-selected', 'true');
	});

	it('lists the names, and the menu notes apart', async () => {
		fetchMock.mockResolvedValue(menu());
		render();
		const row = await screen.findByTestId('dm-proposal');
		expect(row).toHaveTextContent('Promo Videos');
		expect(row).toHaveTextContent('Title 8');
		expect(row).toHaveTextContent('Proposed');
		expect(screen.getByTestId('dm-notes')).toHaveTextContent('Textless OP 00024.mpls: not ripped');
	});

	it('rips a title the menu plays that the job has no track for', async () => {
		const op = { id: 'mnp_op', track_id: null, label: 'Textless OP', plays: '00024.mpls', role: 'other' as const };
		const short = { ...op, scan_title: 19, name: 'Textless OP', note: 'not ripped: 92 s', status: 'info' as const };
		fetchMock.mockResolvedValue(menu({ proposals: [proposal(), proposal(short)] }));
		vi.mocked(ripMenuTitle).mockResolvedValue(proposal({ ...short, track_id: 'trk_19', status: 'accepted' }));
		const onrenamed = render({
			tracks: [...tracks, createTrack({ id: 'trk_19', kind: 'video_title', index: 19, status: 'queued' })]
		});
		const rip = await screen.findByTestId('dm-rip');
		expect(rip).toHaveAttribute('title', 'Rip it from the disc backup as Textless OP');
		await fireEvent.click(rip);
		await waitFor(() => expect(ripMenuTitle).toHaveBeenCalledWith('job_1', 'mnp_op'));
		expect(onrenamed).toHaveBeenCalled();
		expect(screen.queryByTestId('dm-notes')).toBeNull();
		await waitFor(() => expect(screen.getAllByTestId('dm-proposal')).toHaveLength(2));
		expect(screen.getByText('Waiting for the rip')).toBeInTheDocument();
	});

	it('says why it cannot rip, and shows a rip refusal', async () => {
		const short = proposal({ id: 'mnp_op', track_id: null, scan_title: 19, role: 'other', status: 'info' });
		fetchMock.mockResolvedValueOnce(menu({ proposals: [short], rip_refusal: "the job's disc backup isn't kept" }));
		render();
		const rip = await screen.findByTestId('dm-rip');
		expect(rip).toBeDisabled();
		expect(rip).toHaveAttribute('title', "the job's disc backup isn't kept");
		cleanup();

		fetchMock.mockResolvedValueOnce(menu({ proposals: [short] }));
		vi.mocked(ripMenuTitle).mockRejectedValue(new ApiError(409, 'Show/other/Promo Videos.mkv already exists', null));
		const onrenamed = render();
		await fireEvent.click(await screen.findByTestId('dm-rip'));
		expect(await screen.findByTestId('dm-rip-error')).toHaveTextContent('already exists');
		expect(onrenamed).not.toHaveBeenCalled();
		expect(screen.getByTestId('dm-rip')).not.toBeDisabled();
	});

	it('accepts an edited name and tells the page', async () => {
		fetchMock.mockResolvedValue(menu());
		updateMock.mockResolvedValue(
			proposal({ name: 'PV Collection', status: 'applied', applied_path: 'Show/trailers/PV Collection.mkv' })
		);
		const onrenamed = render();
		const name = await screen.findByTestId('dm-name');
		await fireEvent.input(name, { target: { value: 'PV Collection ' } });
		await fireEvent.click(screen.getByTestId('dm-accept'));
		await waitFor(() =>
			expect(updateMock).toHaveBeenCalledWith('job_1', 'mnp_promo', {
				role: 'trailer',
				name: 'PV Collection',
				episode_number: null,
				action: 'accept'
			})
		);
		expect(await screen.findByTestId('dm-applied')).toHaveTextContent('Show/trailers/PV Collection.mkv');
		expect(onrenamed).toHaveBeenCalled();
		expect(screen.queryByTestId('dm-accept')).toBeNull();
	});

	it('needs an episode number for a special', async () => {
		fetchMock.mockResolvedValue(menu());
		render();
		const role = await screen.findByTestId('dm-role');
		await fireEvent.change(role, { target: { value: 'special' } });
		expect(screen.getByTestId('dm-accept')).toBeDisabled();
		expect(screen.getByTestId('dm-accept')).toHaveAttribute('title', 'A special needs its season 0 episode number');
		await fireEvent.input(screen.getByTestId('dm-episode'), { target: { value: '3' } });
		expect(screen.getByTestId('dm-accept')).not.toBeDisabled();
	});

	it('rejects and reopens, and shows a refusal on the row', async () => {
		fetchMock.mockResolvedValue(menu());
		updateMock.mockResolvedValueOnce(proposal({ status: 'rejected' }));
		const onrenamed = render();
		await fireEvent.click(await screen.findByTestId('dm-reject'));
		await waitFor(() => expect(updateMock).toHaveBeenCalledWith('job_1', 'mnp_promo', { action: 'reject' }));
		expect(await screen.findByText('Rejected')).toBeInTheDocument();
		updateMock.mockResolvedValueOnce(proposal());
		await fireEvent.click(screen.getByTestId('dm-reopen'));
		await waitFor(() => expect(updateMock).toHaveBeenLastCalledWith('job_1', 'mnp_promo', { action: 'reopen' }));
		updateMock.mockRejectedValueOnce(new ApiError(409, 'Show/trailers/Promo Videos.mkv already exists', null));
		await fireEvent.click(await screen.findByTestId('dm-accept'));
		expect(await screen.findByTestId('dm-row-error')).toHaveTextContent('already exists');
		expect(onrenamed).not.toHaveBeenCalled();
	});

	it('accepts all, and counts the refusals', async () => {
		fetchMock.mockResolvedValue(
			menu({
				proposals: [
					proposal(),
					proposal({ id: 'mnp_ext', track_id: 'trk_7', label: 'Panel', name: 'Panel', role: 'extra' })
				]
			})
		);
		vi.mocked(acceptAllMenuProposals).mockResolvedValue({
			proposals: [
				proposal({ status: 'applied' }),
				proposal({ id: 'mnp_ext', track_id: 'trk_7', name: 'Panel', role: 'extra' })
			],
			refused: { mnp_ext: 'Show/extras/Panel.mkv already exists' }
		});
		const onrenamed = render();
		const all = await screen.findByTestId('dm-accept-all');
		expect(all).toHaveTextContent('Accept all (2)');
		await fireEvent.click(all);
		expect(await screen.findByTestId('dm-refused')).toHaveTextContent('1 name was not accepted');
		expect(screen.getByTestId('dm-row-error')).toHaveTextContent('already exists');
		expect(onrenamed).toHaveBeenCalled();
	});

	it('shows an accept-all failure', async () => {
		fetchMock.mockResolvedValue(menu());
		vi.mocked(acceptAllMenuProposals).mockRejectedValue(new Error('nope'));
		render();
		await fireEvent.click(await screen.findByTestId('dm-accept-all'));
		expect(await screen.findByTestId('dm-error')).toHaveTextContent('nope');
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
		expect(screen.getAllByTestId('dm-proposal')).toHaveLength(2);
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
		expect(await screen.findByTestId('dm-proposal')).toHaveTextContent('Trailer');
		expect(screen.queryByTestId('dm-accept')).toBeNull();
		expect(screen.queryByTestId('dm-accept-all')).toBeNull();
		expect(screen.queryByTestId('dm-read-again')).toBeNull();
		expect(screen.queryByTestId('dm-hand-open')).toBeNull();
	});

	it('draws the page without its picture when the image fails', async () => {
		vi.mocked(fetchDiscMenuImage).mockRejectedValue(new Error('gone'));
		fetchMock.mockResolvedValue(menu());
		render();
		expect(await screen.findAllByTestId('dm-box')).toHaveLength(1);
		expect(screen.queryByAltText('Menu page 1')).toBeNull();
	});
});
