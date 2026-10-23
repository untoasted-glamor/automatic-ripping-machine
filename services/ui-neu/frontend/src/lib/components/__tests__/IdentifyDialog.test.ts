import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import IdentifyDialog from '../IdentifyDialog.svelte';
import { createJob } from '../__fixtures__/job';
import { resolveJob, searchMetadata, updateJobTitle } from '$lib/api/jobs';
import type { MetadataCandidate, ResolveResponse } from '$lib/types/api.gen';

vi.mock('$lib/api/jobs', () => ({
	resolveJob: vi.fn(),
	searchMetadata: vi.fn(),
	updateJobTitle: vi.fn(() => Promise.resolve({}))
}));

const resolveJobMock = vi.mocked(resolveJob);
const searchMock = vi.mocked(searchMetadata);
const updateJobTitleMock = vi.mocked(updateJobTitle);

function candidate(overrides: Partial<MetadataCandidate> = {}): MetadataCandidate {
	return {
		title: 'ACCA: 13-Territory Inspection Dept.',
		year: 2017,
		kind: 'series',
		poster_url: 'https://img/acca.jpg',
		provider_id: 'tt6401396',
		external_ids: { imdb: 'tt6401396', tmdb: '70881', tvdb: null },
		...overrides
	} as MetadataCandidate;
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
		it('renders title + year inputs and submits title/year with no music', async () => {
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
				expect(resolveJobMock).toHaveBeenCalledWith('job_v', {
					title: 'Blade Runner',
					year: 1982,
					music: undefined
				});
				expect(onidentified).toHaveBeenCalledTimes(1);
			});
		});
	});

	describe('manual search', () => {
		const held = () =>
			createJob({
				id: 'job_s',
				disc_type: 'bluray',
				status: 'awaiting_user_id',
				title: 'ACCA_CS_D1',
				year: null,
				media_type: null,
				poster_url_manual: null
			});

		it('searches, fills title and year from the pick, and resolves with its type, ids and poster', async () => {
			searchMock.mockResolvedValue({ candidates: [candidate(), candidate({ title: 'Other', year: 1999 })] } as never);
			resolveJobMock.mockResolvedValue(makeResp());
			const onidentified = vi.fn();
			renderComponent(IdentifyDialog, { props: { job: held(), onclose: vi.fn(), onidentified } });

			await fireEvent.input(screen.getByTestId('identify-search-query'), { target: { value: 'ACCA' } });
			await fireEvent.change(screen.getByTestId('identify-search-type'), { target: { value: 'tv' } });
			await fireEvent.click(screen.getByTestId('identify-search-run'));
			await waitFor(() => expect(screen.getAllByTestId('identify-candidate')).toHaveLength(2));
			expect(searchMock).toHaveBeenCalledWith('ACCA', 'tv');

			await fireEvent.click(screen.getAllByTestId('identify-candidate')[0]);
			expect((screen.getByTestId('identify-title') as HTMLInputElement).value).toBe(
				'ACCA: 13-Territory Inspection Dept.'
			);
			expect((screen.getByTestId('identify-year') as HTMLInputElement).value).toBe('2017');
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() => expect(onidentified).toHaveBeenCalledTimes(1));
			expect(resolveJobMock).toHaveBeenCalledWith('job_s', {
				title: 'ACCA: 13-Territory Inspection Dept.',
				year: 2017,
				music: undefined,
				media_type: 'tv',
				external_ids: { imdb: 'tt6401396', tmdb: '70881' }
			});
			expect(updateJobTitleMock).toHaveBeenCalledWith('job_s', { poster_url_manual: 'https://img/acca.jpg' });
		});

		it('Enter in the search box searches instead of submitting', async () => {
			searchMock.mockResolvedValue({ candidates: [] } as never);
			renderComponent(IdentifyDialog, { props: { job: held(), onclose: vi.fn(), onidentified: vi.fn() } });

			await fireEvent.keyDown(screen.getByTestId('identify-search-query'), { key: 'Enter' });

			await waitFor(() => expect(screen.getByTestId('identify-search-error')).toHaveTextContent('No results found'));
			expect(resolveJobMock).not.toHaveBeenCalled();
		});

		it('retyping the title drops the pick', async () => {
			searchMock.mockResolvedValue({ candidates: [candidate()] } as never);
			resolveJobMock.mockResolvedValue(makeResp());
			renderComponent(IdentifyDialog, { props: { job: held(), onclose: vi.fn(), onidentified: vi.fn() } });

			await fireEvent.click(screen.getByTestId('identify-search-run'));
			await fireEvent.click(await screen.findByTestId('identify-candidate'));
			await fireEvent.input(screen.getByTestId('identify-title'), { target: { value: 'ACCA' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() =>
				expect(resolveJobMock).toHaveBeenCalledWith('job_s', { title: 'ACCA', year: 2017, music: undefined })
			);
			expect(updateJobTitleMock).not.toHaveBeenCalled();
		});

		it('shows a failed search and no search for a CD', async () => {
			searchMock.mockRejectedValue(new Error('TMDb API key is not configured'));
			renderComponent(IdentifyDialog, { props: { job: held(), onclose: vi.fn(), onidentified: vi.fn() } });
			await fireEvent.click(screen.getByTestId('identify-search-run'));
			await waitFor(() =>
				expect(screen.getByTestId('identify-search-error')).toHaveTextContent('TMDb API key is not configured')
			);
			cleanup();
			renderComponent(IdentifyDialog, {
				props: {
					job: createJob({ disc_type: 'cd', status: 'awaiting_user_id' }),
					onclose: vi.fn(),
					onidentified: vi.fn()
				}
			});
			expect(screen.queryByTestId('identify-search')).toBeNull();
		});

		it('a poster that fails to save still identifies the disc', async () => {
			searchMock.mockResolvedValue({ candidates: [candidate()] } as never);
			resolveJobMock.mockResolvedValue(makeResp());
			updateJobTitleMock.mockRejectedValueOnce(new Error('boom'));
			const onidentified = vi.fn();
			renderComponent(IdentifyDialog, { props: { job: held(), onclose: vi.fn(), onidentified } });
			await fireEvent.click(screen.getByTestId('identify-search-run'));
			await fireEvent.click(await screen.findByTestId('identify-candidate'));
			await fireEvent.click(screen.getByTestId('identify-submit'));
			await waitFor(() => expect(onidentified).toHaveBeenCalledTimes(1));
		});
	});

	describe('identify miss reasons', () => {
		it('lists why automatic identification missed', () => {
			const job = createJob({
				disc_type: 'dvd',
				status: 'awaiting_user_id',
				metadata_json: {
					identify_miss_reasons: ['tmdb: no match for "DISC_1"', 'omdb: skipped (no API key)', 42 as unknown as string]
				}
			});
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
			const box = screen.getByTestId('identify-miss-reasons');
			const items = box.querySelectorAll('li');
			expect(Array.from(items, (li) => li.textContent)).toEqual([
				'tmdb: no match for "DISC_1"',
				'omdb: skipped (no API key)'
			]);
		});

		it('shows nothing when there are none', () => {
			const job = createJob({ disc_type: 'dvd', status: 'awaiting_user_id', metadata_json: {} });
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });
			expect(screen.queryByTestId('identify-miss-reasons')).not.toBeInTheDocument();
		});
	});

	describe('season and disc position', () => {
		it('seeds season / disc # / disc total from the job and omits them when unchanged', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const job = createJob({
				id: 'job_tv',
				disc_type: 'dvd',
				status: 'ripped',
				title: 'Show',
				year: 2020,
				season: 2,
				disc_number: 1,
				disc_total: 3
			});
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });

			expect((screen.getByTestId('identify-season') as HTMLInputElement).value).toBe('2');
			expect((screen.getByTestId('identify-disc-number') as HTMLInputElement).value).toBe('1');
			expect((screen.getByTestId('identify-disc-total') as HTMLInputElement).value).toBe('3');
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() => {
				expect(resolveJobMock).toHaveBeenCalledWith('job_tv', { title: 'Show', year: 2020, music: undefined });
			});
		});

		it('sends a changed season and disc number, and an emptied disc total as null', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const job = createJob({
				id: 'job_tv',
				disc_type: 'bluray',
				status: 'awaiting_user_id',
				title: 'Show',
				year: null,
				season: null,
				disc_number: null,
				disc_total: 4
			});
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });

			await fireEvent.input(screen.getByTestId('identify-season'), { target: { value: '1' } });
			await fireEvent.input(screen.getByTestId('identify-disc-number'), { target: { value: '2' } });
			await fireEvent.input(screen.getByTestId('identify-disc-total'), { target: { value: '' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() => {
				expect(resolveJobMock).toHaveBeenCalledWith('job_tv', {
					title: 'Show',
					year: null,
					music: undefined,
					season: 1,
					disc_number: 2,
					disc_total: null
				});
			});
		});

		it('offers disc # / disc total but no season for a CD', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const job = createJob({ id: 'job_cd', disc_type: 'cd', status: 'awaiting_user_id', title: 'Album' });
			renderComponent(IdentifyDialog, { props: { job, onclose: vi.fn(), onidentified: vi.fn() } });

			expect(screen.queryByTestId('identify-season')).not.toBeInTheDocument();
			await fireEvent.input(screen.getByTestId('identify-artist'), { target: { value: 'Band' } });
			await fireEvent.input(screen.getByTestId('identify-disc-number'), { target: { value: '2' } });
			await fireEvent.input(screen.getByTestId('identify-disc-total'), { target: { value: '2' } });
			await fireEvent.click(screen.getByTestId('identify-submit'));

			await waitFor(() => {
				expect(resolveJobMock).toHaveBeenCalledWith(
					'job_cd',
					expect.objectContaining({ title: 'Album', disc_number: 2, disc_total: 2 })
				);
			});
			expect(resolveJobMock.mock.calls[0][1]).not.toHaveProperty('season');
		});
	});

	describe('music job', () => {
		it('renders album + artist + per-track inputs and submits the music shape', async () => {
			resolveJobMock.mockResolvedValue(makeResp());
			const onidentified = vi.fn();
			const job = createJob({
				id: 'job_cd',
				disc_type: 'cd',
				status: 'awaiting_user_id',
				title: '',
				year: null,
				metadata_json: {
					scan_result: {
						disc_type: 'cd',
						titles: [
							{ index: 0, duration_seconds: 0 },
							{ index: 1, duration_seconds: 0 }
						]
					}
				}
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
					music: {
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
				metadata_json: { scan_result: { disc_type: 'cd', titles: [] } }
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
