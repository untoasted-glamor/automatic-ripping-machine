import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import ReviewGatePanel from '../ReviewGatePanel.svelte';
import { createJob } from '../__fixtures__/job';

vi.mock('$lib/stores/auth', async () => {
	const { derived, writable } = await import('svelte/store');
	const _role = writable<string | null>('admin');
	return {
		role: { subscribe: _role.subscribe },
		isAdmin: derived(_role, (r) => r === 'admin'),
		__setRole: (r: string | null) => _role.set(r)
	};
});

vi.mock('$lib/api/jobs', () => ({
	startWaitingJob: vi.fn(() => Promise.resolve({})),
	retryFailedTitles: vi.fn(() => Promise.resolve({ retried: 1, from_backup: true })),
	pauseWaitingJob: vi.fn(() => Promise.resolve({})),
	fetchTrackSelection: vi.fn(() => Promise.reject(new Error('not loaded'))),
	saveTrackSelection: vi.fn()
}));

import {
	fetchTrackSelection,
	pauseWaitingJob,
	retryFailedTitles,
	saveTrackSelection,
	startWaitingJob
} from '$lib/api/jobs';
import { trackView } from '../__fixtures__/trackSelection';
import * as authStore from '$lib/stores/auth';

const held = (overrides = {}) =>
	createJob({ id: 'job_7', status: 'awaiting_review', wait_start_time: new Date().toISOString(), ...overrides });

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	(authStore as unknown as { __setRole: (r: string | null) => void }).__setRole('admin');
});

describe('ReviewGatePanel', () => {
	it('pauses this disc from the countdown in timed mode', async () => {
		const onchanged = vi.fn();
		renderComponent(ReviewGatePanel, { props: { job: held(), requiresConfirm: false, waitSeconds: 60, onchanged } });
		expect(screen.getByText('The rip starts by itself when the countdown ends.')).toBeInTheDocument();
		await fireEvent.click(screen.getByTitle('Pause timer'));
		await waitFor(() => expect(pauseWaitingJob).toHaveBeenCalledWith('job_7', true));
		await waitFor(() => expect(onchanged).toHaveBeenCalled());
	});

	it('says why a timed countdown is on hold', () => {
		renderComponent(ReviewGatePanel, {
			props: { job: held({ manual_pause: true }), requiresConfirm: false, waitSeconds: 60 }
		});
		expect(screen.getByText('The countdown is paused for this disc.')).toBeInTheDocument();
		cleanup();
		renderComponent(ReviewGatePanel, { props: { job: held(), requiresConfirm: false, waitSeconds: 60, paused: true } });
		expect(screen.getByText('Ripping is paused, so the countdown is on hold.')).toBeInTheDocument();
	});

	it('offers Rip again for a previously-ripped hold, with no countdown or confirm note', async () => {
		renderComponent(ReviewGatePanel, {
			props: {
				job: held({ wait_start_time: null, metadata_json: { previously_ripped: { job_id: 'job_old' } } }),
				requiresConfirm: true,
				waitSeconds: 60
			}
		});
		expect(screen.getByRole('heading', { name: 'Already ripped' })).toBeInTheDocument();
		expect(screen.queryByTestId('review-wait-confirm')).not.toBeInTheDocument();
		await fireEvent.click(screen.getByRole('button', { name: 'Rip again' }));
		await waitFor(() => expect(startWaitingJob).toHaveBeenCalledWith('job_7'));
	});

	it('shows the error when Start is refused', async () => {
		vi.mocked(startWaitingJob).mockRejectedValueOnce(new Error('at least one track must be kept'));
		renderComponent(ReviewGatePanel, { props: { job: held(), requiresConfirm: true, waitSeconds: 60 } });
		await fireEvent.click(screen.getByRole('button', { name: 'Confirm & start rip' }));
		expect(await screen.findByRole('alert')).toHaveTextContent('at least one track must be kept');
	});

	it('guests see the state but no Start', () => {
		(authStore as unknown as { __setRole: (r: string | null) => void }).__setRole('guest');
		renderComponent(ReviewGatePanel, { props: { job: held(), requiresConfirm: true, waitSeconds: 60 } });
		expect(screen.getByTestId('review-wait-confirm')).toBeInTheDocument();
		expect(screen.queryByTestId('review-gate-start')).not.toBeInTheDocument();
	});
});

describe('ReviewGatePanel track choice', () => {
	it('saves the disc track choice before starting the rip', async () => {
		vi.mocked(fetchTrackSelection).mockResolvedValue(trackView());
		const order: string[] = [];
		vi.mocked(saveTrackSelection).mockImplementation(async () => {
			order.push('save');
			return trackView();
		});
		vi.mocked(startWaitingJob).mockImplementation(async () => {
			order.push('start');
			return {} as never;
		});
		renderComponent(ReviewGatePanel, {
			props: { job: held({ disc_type: 'bluray' }), requiresConfirm: true, waitSeconds: 60 }
		});
		await waitFor(() => expect(screen.getByTestId('jtp-custom')).toBeInTheDocument());
		await fireEvent.click(screen.getByTestId('jtp-custom'));
		await fireEvent.click(screen.getByTestId('jtp-disc-audio-eng'));
		await fireEvent.click(screen.getByTestId('review-gate-start'));
		await waitFor(() => expect(order).toEqual(['save', 'start']));
		expect(vi.mocked(saveTrackSelection).mock.calls[0][1]).toEqual(
			expect.objectContaining({ audio_languages: ['jpn'] })
		);
	});

	it('does not start when the choice cannot be saved, and starts unchanged without saving', async () => {
		vi.mocked(fetchTrackSelection).mockResolvedValue(trackView());
		renderComponent(ReviewGatePanel, {
			props: { job: held({ disc_type: 'dvd' }), requiresConfirm: true, waitSeconds: 60 }
		});
		await waitFor(() => expect(screen.getByTestId('jtp-custom')).toBeInTheDocument());
		await fireEvent.click(screen.getByTestId('review-gate-start'));
		await waitFor(() => expect(startWaitingJob).toHaveBeenCalled());
		expect(saveTrackSelection).not.toHaveBeenCalled();

		vi.mocked(startWaitingJob).mockClear();
		await fireEvent.click(screen.getByTestId('jtp-custom'));
		await fireEvent.change(screen.getByTestId('jtp-audio-selection'), { target: { value: 'languages' } });
		await fireEvent.click(screen.getByTestId('review-gate-start'));
		await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Add at least one language'));
		expect(startWaitingJob).not.toHaveBeenCalled();
	});

	it("a video disc's track choice starts closed", async () => {
		renderComponent(ReviewGatePanel, { props: { job: held(), requiresConfirm: true, waitSeconds: 60 } });
		const tracks = await screen.findByTestId('review-gate-tracks');
		expect(tracks).not.toHaveAttribute('open');
	});

	it('shows no track choice for a CD', () => {
		renderComponent(ReviewGatePanel, {
			props: { job: held({ disc_type: 'cd' }), requiresConfirm: true, waitSeconds: 60 }
		});
		expect(screen.queryByTestId('review-gate-tracks')).toBeNull();
	});
});

describe('ReviewGatePanel, a disc backed up whole first (fork only)', () => {
	it('stop 1 starts the backup, with no title to tick yet', async () => {
		const onchanged = vi.fn();
		renderComponent(ReviewGatePanel, {
			props: { job: held(), requiresConfirm: true, waitSeconds: 60, keptCount: 0, parksAfterBackup: true, onchanged }
		});
		expect(screen.getByText(/its titles are chosen after the backup/)).toBeInTheDocument();
		expect(screen.queryByTestId('review-gate-nothing-kept')).not.toBeInTheDocument();
		const start = screen.getByTestId('review-gate-start');
		expect(start).toHaveTextContent('Begin ripping disc to backup/menu');
		await fireEvent.click(start);
		await waitFor(() => expect(startWaitingJob).toHaveBeenCalledWith('job_7'));
		expect(retryFailedTitles).not.toHaveBeenCalled();
	});

	it('stop 1 in timed mode says the backup starts when the countdown ends', () => {
		renderComponent(ReviewGatePanel, {
			props: { job: held(), requiresConfirm: false, waitSeconds: 60, parksAfterBackup: true }
		});
		expect(
			screen.getByText('The disc is backed up when the countdown ends. Its titles are chosen after.')
		).toBeInTheDocument();
	});

	const stop2 = (overrides = {}) => held({ status: 'awaiting_titles', ...overrides });

	it('stop 2 reads the disc menu before it opens: no countdown and no start', () => {
		renderComponent(ReviewGatePanel, {
			props: { job: stop2({ wait_start_time: null }), requiresConfirm: false, waitSeconds: 60, keptCount: 2 }
		});
		expect(screen.getByRole('heading', { name: 'Choose the titles' })).toBeInTheDocument();
		expect(screen.getByText(/Reading the disc menu/)).toBeInTheDocument();
		expect(screen.queryByTestId('review-gate-start')).not.toBeInTheDocument();
		expect(screen.queryByTitle('Pause timer')).not.toBeInTheDocument();
	});

	it('an open stop 2 rips the ticked titles from the backup, with no audio/subtitle picker', async () => {
		const onchanged = vi.fn();
		renderComponent(ReviewGatePanel, {
			props: { job: stop2(), requiresConfirm: true, waitSeconds: 60, keptCount: 3, onchanged }
		});
		expect(screen.getByText(/Tick the ones to keep in the Tracks table/)).toBeInTheDocument();
		expect(screen.queryByTestId('review-gate-tracks')).not.toBeInTheDocument();
		const start = screen.getByTestId('review-gate-start');
		expect(start).toHaveTextContent('Rip titles');
		await fireEvent.click(start);
		await waitFor(() => expect(retryFailedTitles).toHaveBeenCalledWith('job_7'));
		expect(startWaitingJob).not.toHaveBeenCalled();
		await waitFor(() => expect(onchanged).toHaveBeenCalled());
	});

	it('an open stop 2 counts down and pauses like stop 1', async () => {
		renderComponent(ReviewGatePanel, {
			props: { job: stop2(), requiresConfirm: false, waitSeconds: 60, keptCount: 1 }
		});
		expect(
			screen.getByText('The ticked titles are ripped from the disc backup when the countdown ends.')
		).toBeInTheDocument();
		await fireEvent.click(screen.getByTitle('Pause timer'));
		await waitFor(() => expect(pauseWaitingJob).toHaveBeenCalledWith('job_7', true));
	});

	it('stop 2 with nothing ticked waits for a title', () => {
		renderComponent(ReviewGatePanel, { props: { job: stop2(), requiresConfirm: true, waitSeconds: 60, keptCount: 0 } });
		expect(screen.getByTestId('review-gate-nothing-kept')).toBeInTheDocument();
		expect(screen.getByTestId('review-gate-start')).toBeDisabled();
	});
});
