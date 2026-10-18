import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import IdentifyDialog from '../IdentifyDialog.svelte';
import { createJob } from '../__fixtures__/job';
import { resolveJob, searchMetadata, startWaitingJob, pauseWaitingJob } from '$lib/api/jobs';
import { fetchConfigView } from '$lib/api/config';
import type { ConfigView, ResolveResponse } from '$lib/types/api.gen';

vi.mock('$lib/api/jobs', () => ({
	resolveJob: vi.fn(),
	searchMetadata: vi.fn(),
	startWaitingJob: vi.fn(),
	pauseWaitingJob: vi.fn()
}));

vi.mock('$lib/api/config', () => ({
	fetchConfigView: vi.fn()
}));

const resolveJobMock = vi.mocked(resolveJob);
const searchMetadataMock = vi.mocked(searchMetadata);
const startWaitingJobMock = vi.mocked(startWaitingJob);
const pauseWaitingJobMock = vi.mocked(pauseWaitingJob);
const fetchConfigViewMock = vi.mocked(fetchConfigView);

function configWith(manual_wait_seconds: number | null): ConfigView {
	return { manual_wait_seconds } as ConfigView;
}

function makeResp(): ResolveResponse {
	return { job: createJob({ status: 'identified' }), fan_out: [] };
}

describe('IdentifyDialog', () => {
	afterEach(() => {
		cleanup();
		vi.clearAllMocks();
	});

	describe('video job', () => {
		it('renders title + year inputs and submits title/year with no metadata', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const onidentified = vi.fn();
			const job = createJob({
				id: 'job_v',
				disc_type: 'bluray',
				status: 'awaiting_user_id',
				title: '',
				year: null
			});
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified }
			});

			const titleInput = screen.getByTestId('identify-title') as HTMLInputElement;
			const yearInput = screen.getByTestId('identify-year') as HTMLInputElement;
			expect(titleInput).toBeInTheDocument();
			expect(yearInput).toBeInTheDocument();

			await fireEvent.input(titleInput, { target: { value: 'Blade Runner' } });
			await fireEvent.input(yearInput, { target: { value: '1982' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() => {
				// season/disc ride along as explicit nulls: resolve assigns them
				// unconditionally, so omitting them would clear whatever was set before.
				expect(resolveJobMock).toHaveBeenCalledWith('job_v', {
					title: 'Blade Runner',
					year: 1982,
					season_number: null,
					disc_number: null,
					disc_total: null,
					metadata: undefined
				});
				expect(onidentified).toHaveBeenCalledTimes(1);
			});
		});
	});

	describe('season / disc box-set fields', () => {
		it('sends the season/disc typed into the form', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const job = createJob({ id: 'job_s', disc_type: 'bluray', status: 'awaiting_user_id', title: '' });
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });

			await fireEvent.input(screen.getByTestId('identify-title'), { target: { value: 'ACCA' } });
			await fireEvent.input(screen.getByTestId('identify-season-number'), { target: { value: '1' } });
			await fireEvent.input(screen.getByTestId('identify-disc-number'), { target: { value: '2' } });
			await fireEvent.input(screen.getByTestId('identify-disc-total'), { target: { value: '3' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() =>
				expect(resolveJobMock).toHaveBeenCalledWith(
					'job_s',
					expect.objectContaining({ season_number: 1, disc_number: 2, disc_total: 3 })
				)
			);
		});

		it('seeds season/disc from the job so an edit does not clear them', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const job = createJob({
				id: 'job_s2',
				disc_type: 'bluray',
				status: 'identified',
				title: 'ACCA',
				season_number: 1,
				disc_number: 2,
				disc_total: 3
			});
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() =>
				expect(resolveJobMock).toHaveBeenCalledWith(
					'job_s2',
					expect.objectContaining({ season_number: 1, disc_number: 2, disc_total: 3 })
				)
			);
		});

		it('offers disc (not season) fields on a music disc and sends them', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const job = createJob({
				id: 'job_cd2',
				disc_type: 'cd',
				status: 'awaiting_user_id',
				title: 'Album',
				metadata_json: {}
			});
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
			expect(screen.queryByTestId('identify-season-number')).not.toBeInTheDocument();
			await fireEvent.input(screen.getByTestId('identify-artist'), { target: { value: 'A' } });
			await fireEvent.input(screen.getByTestId('identify-disc-number'), { target: { value: '1' } });
			await fireEvent.input(screen.getByTestId('identify-disc-total'), { target: { value: '2' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() =>
				expect(resolveJobMock).toHaveBeenCalledWith(
					'job_cd2',
					expect.objectContaining({ season_number: null, disc_number: 1, disc_total: 2 })
				)
			);
		});
	});

	describe('manual title search', () => {
		const candidate = {
			title: 'ACCA: 13-Territory Inspection Dept.',
			year: 2017,
			kind: 'tv',
			poster_url: 'https://img/acca.jpg',
			provider_id: 'tmdb:123'
		};

		function renderVideo(overrides: Record<string, unknown> = {}) {
			const job = createJob({
				id: 'job_q',
				disc_type: 'bluray',
				status: 'awaiting_user_id',
				title: 'ACCA CS D2',
				year: null,
				...overrides
			});
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
		}

		it('is seeded with the job title and searches the chosen type', async () => {
			searchMetadataMock.mockResolvedValue({ candidates: [candidate] });
			renderVideo();
			const input = screen.getByTestId('search-title') as HTMLInputElement;
			expect(input.value).toBe('ACCA CS D2');
			await fireEvent.input(input, { target: { value: 'ACCA' } });
			await fireEvent.change(screen.getByTestId('search-type'), { target: { value: 'tv' } });
			await fireEvent.click(screen.getByTestId('search-run'));
			await waitFor(() => expect(searchMetadataMock).toHaveBeenCalledWith('ACCA', 'tv'));
			expect(await screen.findByTestId('identify-candidate-tmdb:123')).toBeInTheDocument();
		});

		it('searches on Enter in the search box', async () => {
			searchMetadataMock.mockResolvedValue({ candidates: [] });
			renderVideo();
			await fireEvent.keyDown(screen.getByTestId('search-title'), { key: 'Enter' });
			await waitFor(() => expect(searchMetadataMock).toHaveBeenCalledWith('ACCA CS D2', 'movie'));
		});

		it('shows the provider detail (or "No results") when nothing matches', async () => {
			searchMetadataMock.mockResolvedValueOnce({ candidates: [], detail: 'TMDB key rejected' });
			renderVideo();
			await fireEvent.click(screen.getByTestId('search-run'));
			expect(await screen.findByTestId('search-error')).toHaveTextContent('TMDB key rejected');

			searchMetadataMock.mockResolvedValueOnce({ candidates: [] });
			await fireEvent.click(screen.getByTestId('search-run'));
			await waitFor(() => expect(screen.getByTestId('search-error')).toHaveTextContent('No results'));
		});

		it('shows the error when the search request fails', async () => {
			searchMetadataMock.mockRejectedValue(new Error('provider down'));
			renderVideo();
			await fireEvent.click(screen.getByTestId('search-run'));
			expect(await screen.findByTestId('search-error')).toHaveTextContent('provider down');
		});

		it('picking a candidate fills title/year and submits its id, kind and poster', async () => {
			searchMetadataMock.mockResolvedValue({ candidates: [candidate] });
			resolveJobMock.mockResolvedValue(makeResp());
			renderVideo();
			await fireEvent.click(screen.getByTestId('search-run'));
			const pick = await screen.findByTestId('identify-candidate-tmdb:123');
			await fireEvent.click(pick);
			expect(pick).toHaveAttribute('aria-pressed', 'true');
			expect((screen.getByTestId('identify-title') as HTMLInputElement).value).toBe(candidate.title);
			expect((screen.getByTestId('identify-year') as HTMLInputElement).value).toBe('2017');

			await fireEvent.click(screen.getByTestId('identify-submit'));
			await waitFor(() =>
				expect(resolveJobMock).toHaveBeenCalledWith('job_q', {
					title: candidate.title,
					year: 2017,
					season_number: null,
					disc_number: null,
					disc_total: null,
					metadata: { provider_id: 'tmdb:123', poster_url: 'https://img/acca.jpg', kind: 'tv' }
				})
			);
		});

		it('drops the picked candidate metadata once the title is retyped', async () => {
			searchMetadataMock.mockResolvedValue({ candidates: [candidate] });
			resolveJobMock.mockResolvedValue(makeResp());
			renderVideo();
			await fireEvent.click(screen.getByTestId('search-run'));
			await fireEvent.click(await screen.findByTestId('identify-candidate-tmdb:123'));
			await fireEvent.input(screen.getByTestId('identify-title'), { target: { value: 'Something else' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));
			await waitFor(() =>
				expect(resolveJobMock).toHaveBeenCalledWith(
					'job_q',
					expect.objectContaining({ title: 'Something else', metadata: undefined })
				)
			);
		});

		it('is not offered on a music disc', () => {
			const job = createJob({ disc_type: 'cd', status: 'awaiting_user_id', metadata_json: {} });
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
			expect(screen.queryByTestId('search-title')).not.toBeInTheDocument();
		});
	});

	describe('review gate (awaiting_review)', () => {
		function renderReview(
			overrides: Record<string, unknown> = {},
			handlers: Record<string, unknown> = {}
		) {
			const job = createJob({
				id: 'job_r',
				disc_type: 'bluray',
				status: 'awaiting_review',
				title: 'Heat',
				year: 1995,
				...overrides
			});
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn(), ...handlers }
			});
		}

		it('is not shown outside the review gate', () => {
			const job = createJob({ disc_type: 'bluray', status: 'ripped' });
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
			expect(screen.queryByTestId('confirm-start-rip')).not.toBeInTheDocument();
			expect(fetchConfigViewMock).not.toHaveBeenCalled();
		});

		it('titles the dialog for review and offers Confirm & start rip', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(null));
			renderReview();
			expect(screen.getByRole('heading', { name: 'Review & confirm identity' })).toBeInTheDocument();
			expect(screen.getByTestId('confirm-start-rip')).toHaveTextContent('Confirm & start rip');
		});

		it('Confirm & start rip calls rip-start-review and reports the started job', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(null));
			const started = createJob({ id: 'job_r', status: 'ripping' });
			startWaitingJobMock.mockResolvedValue(started);
			const onripstarted = vi.fn();
			renderReview({}, { onripstarted });
			await fireEvent.click(screen.getByTestId('confirm-start-rip'));
			await waitFor(() => expect(startWaitingJobMock).toHaveBeenCalledWith('job_r'));
			await waitFor(() => expect(onripstarted).toHaveBeenCalledWith(started));
		});

		it('shows the error when starting the rip fails', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(null));
			startWaitingJobMock.mockRejectedValue(new Error('drive busy'));
			renderReview();
			await fireEvent.click(screen.getByTestId('confirm-start-rip'));
			expect(await screen.findByTestId('identify-error')).toHaveTextContent('drive busy');
		});

		it('mandatory mode has no countdown to pause', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(null));
			renderReview();
			expect(await screen.findByTestId('review-mandatory-note')).toBeInTheDocument();
			expect(screen.queryByTestId('review-pause-toggle')).not.toBeInTheDocument();
		});

		it('timed mode offers a pause toggle that pauses this disc', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(60));
			const paused = createJob({ id: 'job_r', status: 'awaiting_review', manual_pause: true });
			pauseWaitingJobMock.mockResolvedValue(paused);
			const onjobupdated = vi.fn();
			renderReview({ manual_pause: false }, { onjobupdated });
			const toggle = await screen.findByTestId('review-pause-toggle');
			expect(toggle).toHaveTextContent('Pause countdown');
			await fireEvent.click(toggle);
			await waitFor(() => expect(pauseWaitingJobMock).toHaveBeenCalledWith('job_r', true));
			expect(onjobupdated).toHaveBeenCalledWith(paused);
		});

		it('timed mode offers Resume on a paused disc', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(60));
			pauseWaitingJobMock.mockResolvedValue(createJob({ id: 'job_r' }));
			renderReview({ manual_pause: true });
			const toggle = await screen.findByTestId('review-pause-toggle');
			expect(toggle).toHaveTextContent('Resume countdown');
			expect(screen.getByText('Countdown paused')).toBeInTheDocument();
			await fireEvent.click(toggle);
			await waitFor(() => expect(pauseWaitingJobMock).toHaveBeenCalledWith('job_r', false));
		});

		it('shows the error when toggling the pause fails', async () => {
			fetchConfigViewMock.mockResolvedValue(configWith(60));
			pauseWaitingJobMock.mockRejectedValue(new Error('nope'));
			renderReview();
			await fireEvent.click(await screen.findByTestId('review-pause-toggle'));
			expect(await screen.findByTestId('identify-error')).toHaveTextContent('nope');
		});

		it('renders no pause control when the config fetch fails', async () => {
			fetchConfigViewMock.mockRejectedValue(new Error('offline'));
			renderReview();
			await waitFor(() => expect(fetchConfigViewMock).toHaveBeenCalled());
			expect(screen.queryByTestId('review-pause-toggle')).not.toBeInTheDocument();
			expect(screen.getByTestId('confirm-start-rip')).toBeInTheDocument();
		});
	});

	describe('music job', () => {
		it('renders album + artist + per-track inputs and submits the music metadata shape', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const onidentified = vi.fn();
			const job = createJob({
				id: 'job_cd',
				disc_type: 'cd',
				status: 'awaiting_user_id',
				title: '',
				year: null,
				metadata_json: { scan_result: { titles: [{}, {}] } }
			});
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified }
			});

			const albumInput = screen.getByTestId('identify-album') as HTMLInputElement;
			const artistInput = screen.getByTestId('identify-artist') as HTMLInputElement;
			expect(albumInput).toBeInTheDocument();
			expect(artistInput).toBeInTheDocument();

			const track1 = screen.getByTestId('identify-track-1') as HTMLInputElement;
			const track2 = screen.getByTestId('identify-track-2') as HTMLInputElement;
			expect(track1).toBeInTheDocument();
			expect(track2).toBeInTheDocument();

			await fireEvent.input(albumInput, { target: { value: 'Dark Side' } });
			await fireEvent.input(artistInput, { target: { value: 'Pink Floyd' } });
			await fireEvent.input(track1, { target: { value: 'Speak to Me' } });
			await fireEvent.input(track2, { target: { value: 'Breathe' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() => {
				expect(resolveJobMock).toHaveBeenCalledWith('job_cd', {
					title: 'Dark Side',
					year: null,
					season_number: null,
					disc_number: null,
					disc_total: null,
					metadata: {
						artist: 'Pink Floyd',
						album: 'Dark Side',
						tracks: [{ title: 'Speak to Me' }, { title: 'Breathe' }]
					}
				});
				expect(onidentified).toHaveBeenCalledTimes(1);
			});
		});

		it('shows the helper text and no track inputs when scan_result has no titles', () => {
			const job = createJob({
				id: 'job_cd0',
				disc_type: 'cd',
				status: 'awaiting_user_id',
				metadata_json: {}
			});
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn() }
			});
			expect(screen.queryByTestId('identify-track-1')).not.toBeInTheDocument();
			expect(screen.getByText(/Track count couldn't be determined/i)).toBeInTheDocument();
		});
	});

	describe('button label', () => {
		it('reads "Identify disc" for awaiting_user_id', () => {
			const job = createJob({ disc_type: 'bluray', status: 'awaiting_user_id' });
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn() }
			});
			expect(screen.getByTestId('identify-submit')).toHaveTextContent('Identify disc');
		});

		it('reads "Edit identity" for a post-rip status (ripped)', () => {
			const job = createJob({ disc_type: 'bluray', status: 'ripped' });
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn() }
			});
			expect(screen.getByTestId('identify-submit')).toHaveTextContent('Edit identity');
		});
	});

	describe('canSubmit', () => {
		it('disables submit for video when title is empty', () => {
			const job = createJob({ disc_type: 'bluray', status: 'awaiting_user_id', title: '' });
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn() }
			});
			expect(screen.getByTestId('identify-submit')).toBeDisabled();
		});

		it('disables submit for music until both album and artist are filled', async () => {
			const job = createJob({
				disc_type: 'cd',
				status: 'awaiting_user_id',
				title: '',
				metadata_json: { scan_result: { titles: [] } }
			});
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn() }
			});
			const submit = screen.getByTestId('identify-submit');
			expect(submit).toBeDisabled();
			await fireEvent.input(screen.getByTestId('identify-album'), { target: { value: 'A' } });
			expect(submit).toBeDisabled();
			await fireEvent.input(screen.getByTestId('identify-artist'), { target: { value: 'B' } });
			expect(submit).not.toBeDisabled();
		});
	});

	describe('error handling', () => {
		it('shows the thrown error message inline', async () => {
			resolveJobMock.mockRejectedValue(new Error('boom'));
			const job = createJob({ disc_type: 'bluray', status: 'awaiting_user_id', title: 'X' });
			renderComponent(IdentifyDialog, {
				props: { job, onclose: vi.fn(), onidentified: vi.fn() }
			});
			await fireEvent.click(screen.getByTestId('identify-submit'));
			await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument());
		});
	});

	describe('cancel', () => {
		it('fires onclose', async () => {
			const onclose = vi.fn();
			const job = createJob({ disc_type: 'bluray', status: 'awaiting_user_id' });
			renderComponent(IdentifyDialog, {
				props: { job, onclose, onidentified: vi.fn() }
			});
			await fireEvent.click(screen.getByText('Cancel'));
			expect(onclose).toHaveBeenCalledTimes(1);
		});
	});
});
