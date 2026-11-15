import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, cleanup, fireEvent, waitFor } from '$lib/test-utils';

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
	resolveJob: vi.fn(),
	searchMetadata: vi.fn(),
	fetchMediaDetail: vi.fn(),
	updateJobTitle: vi.fn(),
	setJobMediaType: vi.fn(),
	fetchNamingPreview: vi.fn(() => Promise.resolve({ job_output_dir: '', job_output_name: '', items: [] })),
	updateTrack: vi.fn()
}));
vi.mock('$lib/api/identity', () => ({
	fetchIdentity: vi.fn(() => Promise.resolve({ sources: {}, pin: null, tracks: [] })),
	matchIdentity: vi.fn(),
	unpinIdentity: vi.fn(),
	fetchEpisodes: vi.fn()
}));

import IdentityPanel from '../IdentityPanel.svelte';
import { createJob } from '$lib/components/__fixtures__/job';
import { resolveJob } from '$lib/api/jobs';
import type { JobStatus, JobView } from '$lib/types/api.gen';

async function setRole(r: string | null) {
	((await import('$lib/stores/auth')) as unknown as { __setRole: (r: string | null) => void }).__setRole(r);
}

function render(job: JobView, extra: Record<string, unknown> = {}) {
	const props = {
		job,
		tracks: [],
		handSetCount: 0,
		onchanged: vi.fn(),
		onseries: vi.fn(),
		ontypechanged: vi.fn(),
		onmanual: vi.fn(),
		...extra
	};
	renderComponent(IdentityPanel, { props });
	return props;
}

const toggle = () => screen.getByTestId('identity-toggle');

describe('IdentityPanel', () => {
	afterEach(async () => {
		cleanup();
		vi.clearAllMocks();
		await setRole('admin');
	});

	it.each<[JobStatus, boolean]>([
		['awaiting_user_id', true],
		['awaiting_review', true],
		['awaiting_titles', true],
		['ripped_awaiting_identify', true],
		['identified', false],
		['ripped', false]
	])('at %s it opens by itself: %s', (status, opens) => {
		render(createJob({ status }));
		expect(toggle()).toHaveAttribute('aria-expanded', String(opens));
		expect(!!screen.queryByTestId('identity-step-search')).toBe(opens);
	});

	it('the toggle opens and closes it', async () => {
		render(createJob({ status: 'ripped' }));
		await fireEvent.click(toggle());
		expect(screen.getByTestId('identity-step-search')).toBeInTheDocument();
		await fireEvent.click(toggle());
		expect(screen.queryByTestId('identity-step-search')).not.toBeInTheDocument();
	});

	it('a disc waiting for its identity is identified by a pick or by hand, not by its season and disc', async () => {
		const props = render(createJob({ status: 'awaiting_user_id', media_type: 'tv' }));
		expect(screen.queryByTestId('identity-step-position')).not.toBeInTheDocument();
		const manual = screen.getByTestId('identify-manual');
		expect(manual).toHaveTextContent('Identify by hand');
		await fireEvent.click(manual);
		expect(props.onmanual).toHaveBeenCalledOnce();
	});

	it('saves the season and disc through resolve, with only the changed fields', async () => {
		vi.mocked(resolveJob).mockResolvedValue({ job: createJob(), fan_out: [] });
		const job = createJob({ status: 'awaiting_titles', media_type: 'tv', title: 'Hanako', year: 2020, season: 1 });
		const props = render(job);
		const save = screen.getByTestId('identity-position-save');
		expect(save).toBeDisabled();
		await fireEvent.input(screen.getByTestId('identify-disc-number'), { target: { value: '2' } });
		expect(save).toBeEnabled();
		await fireEvent.click(save);
		await waitFor(() => expect(props.onchanged).toHaveBeenCalledOnce());
		expect(resolveJob).toHaveBeenCalledWith(job.id, { title: 'Hanako', year: 2020, disc_number: 2 });
	});

	it('shows why the season and disc were not saved', async () => {
		vi.mocked(resolveJob).mockRejectedValue(new Error('409 Conflict'));
		const props = render(createJob({ status: 'ripped', media_type: 'movie' }));
		await fireEvent.click(toggle());
		expect(screen.getByTestId('identity-step-position')).toHaveTextContent('Disc');
		expect(screen.queryByTestId('identify-season')).toBeInTheDocument();
		await fireEvent.input(screen.getByTestId('identify-disc-total'), { target: { value: '4' } });
		await fireEvent.click(screen.getByTestId('identity-position-save'));
		expect(await screen.findByRole('alert')).toHaveTextContent('409 Conflict');
		expect(props.onchanged).not.toHaveBeenCalled();
	});

	it('a job that cannot be resolved (ripping) keeps the search, for its poster, and nothing else', async () => {
		render(createJob({ status: 'ripping', media_type: 'tv' }));
		await fireEvent.click(toggle());
		expect(screen.getByTestId('identity-step-search')).toBeInTheDocument();
		expect(screen.queryByTestId('identity-step-position')).not.toBeInTheDocument();
		expect(screen.queryByTestId('identify-manual')).not.toBeInTheDocument();
		expect(screen.getByTestId('identity-step-episodes')).toBeInTheDocument();
	});

	it('a guest reads it: no switch, no saves', async () => {
		await setRole('guest');
		render(createJob({ status: 'awaiting_review', media_type: 'tv' }));
		expect(screen.queryByRole('radiogroup', { name: 'Media type' })).not.toBeInTheDocument();
		expect(screen.queryByTestId('identify-manual')).not.toBeInTheDocument();
		expect(screen.queryByTestId('identity-step-position')).not.toBeInTheDocument();
		expect(screen.getByTestId('identity-step-episodes')).toBeInTheDocument();
	});

	it("a CD's panel is the music match", () => {
		render(createJob({ status: 'awaiting_user_id', disc_type: 'cd' }));
		expect(screen.getByTestId('identity-step-music')).toBeInTheDocument();
		expect(screen.queryByTestId('identity-step-search')).not.toBeInTheDocument();
		expect(screen.queryByRole('radiogroup', { name: 'Media type' })).not.toBeInTheDocument();
	});
});
