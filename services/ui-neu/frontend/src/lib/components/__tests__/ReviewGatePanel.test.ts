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
	pauseWaitingJob: vi.fn(() => Promise.resolve({}))
}));

import { pauseWaitingJob, startWaitingJob } from '$lib/api/jobs';
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
