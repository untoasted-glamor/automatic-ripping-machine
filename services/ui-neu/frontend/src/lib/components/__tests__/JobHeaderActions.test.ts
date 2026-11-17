import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, waitFor, cleanup } from '$lib/test-utils';
import JobHeaderActions from '../JobHeaderActions.svelte';
import { createJob, createJobDetail } from '$lib/components/__fixtures__/job';
import { JobDetail } from '$lib/stores/jobDetail.svelte';
import * as authStore from '$lib/stores/auth';
import { abandonJob, deleteJob, setJobMediaType } from '$lib/api/jobs';
import type { JobView, TrackView } from '$lib/types/api.gen';

vi.mock('$lib/stores/auth', async () => {
	const { derived, writable } = await import('svelte/store');
	const _role = writable<string | null>('admin');
	return {
		role: { subscribe: _role.subscribe },
		isAdmin: derived(_role, (r) => r === 'admin'),
		// Test-only helper - not part of the real module's public API.
		__setRole: (r: string | null) => _role.set(r)
	};
});

vi.mock('$lib/api/jobs', () => ({
	setJobMediaType: vi.fn(),
	abandonJob: vi.fn(),
	deleteJob: vi.fn(),
	ripAgain: vi.fn()
}));

const setRole = (authStore as unknown as { __setRole: (r: string | null) => void }).__setRole;

function render(job: JobView, tracks: TrackView[] = [], extra: { hasIdentityPanel?: boolean } = {}) {
	const jobDetail = new JobDetail(() => 'job_1');
	jobDetail.detail = createJobDetail({ ...job, tracks });
	const load = vi.spyOn(jobDetail, 'load').mockResolvedValue(undefined);
	const handlers = {
		onidentify: vi.fn(),
		onapply: vi.fn(),
		ondelete: vi.fn()
	};
	renderComponent(JobHeaderActions, {
		job: jobDetail.detail.job,
		jobDetail,
		hasIdentityPanel: extra.hasIdentityPanel ?? false,
		subtitleOcr: null,
		ontextsubtitles: vi.fn(),
		...handlers
	});
	return { ...handlers, load };
}

beforeEach(() => {
	setRole('admin');
	vi.mocked(setJobMediaType).mockResolvedValue(undefined as never);
});
afterEach(cleanup);

describe('JobHeaderActions', () => {
	it('labels the identify button Edit identity on a CD with an identity and reports a click', async () => {
		const { onidentify } = render(createJob({ disc_type: 'cd', status: 'ripped' }));
		await fireEvent.click(screen.getByTestId('identify-open'));
		expect(screen.getByTestId('identify-open')).toHaveTextContent('Edit identity');
		expect(onidentify).toHaveBeenCalledTimes(1);
	});

	it('labels the identify button Identify disc on a disc awaiting an identity', () => {
		render(createJob({ disc_type: 'dvd', status: 'awaiting_user_id' }));
		expect(screen.getByTestId('identify-open')).toHaveTextContent('Identify disc');
	});

	// The fork's video and CD jobs have the Identity panel, which holds the media
	// type switch (its tests are in IdentityPanel.test.ts); the button opens the
	// panel at any status.
	it('labels the button Identity and shows it at any status when the job has the Identity panel', () => {
		render(createJob({ disc_type: 'dvd', status: 'identified' }), [], { hasIdentityPanel: true });
		expect(screen.getByTestId('identify-open')).toHaveTextContent('Identity');
	});

	it('labels the button Edit identity on an identified job without the Identity panel', () => {
		render(createJob({ disc_type: 'dvd', status: 'identified' }));
		expect(screen.getByTestId('identify-open')).toHaveTextContent('Edit identity');
	});

	it('hides the identify button when the job cannot be resolved', () => {
		render(createJob({ disc_type: 'cd', status: 'ripping' }));
		expect(screen.queryByTestId('identify-open')).toBeNull();
	});

	it('shows the apply button when the job can be applied and reports a click', async () => {
		const { onapply } = render(createJob({ status: 'ripped' }));
		await fireEvent.click(screen.getByTestId('apply-open'));
		expect(onapply).toHaveBeenCalledTimes(1);
	});

	it('hides the apply button when the job cannot be applied', () => {
		render(createJob({ status: 'ripping' }));
		expect(screen.queryByTestId('apply-open')).toBeNull();
	});

	it('shows the job actions to an admin', () => {
		render(createJob({ status: 'ripping' }));
		expect(screen.getByRole('button', { name: /abandon/i })).toBeInTheDocument();
	});

	it('reloads the job after an action', async () => {
		vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
		vi.mocked(abandonJob).mockResolvedValue(undefined as never);
		const { load } = render(createJob({ status: 'ripping' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Abandon' }));
		await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
		vi.unstubAllGlobals();
	});

	it('reports a delete', async () => {
		vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
		vi.mocked(deleteJob).mockResolvedValue(undefined as never);
		const { ondelete } = render(createJob({ status: 'ripped' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(ondelete).toHaveBeenCalledTimes(1));
		vi.unstubAllGlobals();
	});

	it('shows a guest none of the actions', () => {
		setRole('guest');
		render(createJob({ disc_type: 'dvd', status: 'awaiting_user_id' }));
		expect(screen.queryByRole('radiogroup', { name: 'Media type' })).toBeNull();
		expect(screen.queryByTestId('identify-open')).toBeNull();
		expect(screen.queryByTestId('apply-open')).toBeNull();
		expect(screen.queryByRole('button')).toBeNull();
	});
});
