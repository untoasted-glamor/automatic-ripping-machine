import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import JobTrackPicker from '../JobTrackPicker.svelte';
import { trackView } from '../__fixtures__/trackSelection';

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
	fetchTrackSelection: vi.fn(),
	saveTrackSelection: vi.fn()
}));

import { fetchTrackSelection, saveTrackSelection } from '$lib/api/jobs';
import * as authStore from '$lib/stores/auth';

const fetchMock = vi.mocked(fetchTrackSelection);
const saveMock = vi.mocked(saveTrackSelection);

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	(authStore as unknown as { __setRole: (r: string | null) => void }).__setRole('admin');
});

function render(props: Record<string, unknown> = {}) {
	return renderComponent(JobTrackPicker, { props: { jobId: 'job_1', variant: 'page', ...props } });
}

describe('JobTrackPicker', () => {
	it('shows the default and the disc, read only, until the operator chooses', async () => {
		fetchMock.mockResolvedValue(trackView());
		render();
		expect(screen.getByTestId('jtp-loading')).toBeInTheDocument();
		await waitFor(() => expect(screen.getByTestId('jtp-default')).toBeInTheDocument());
		expect(screen.getByTestId('jtp-default')).toHaveTextContent(
			'Default (Plex TV 1080p H.265): Every audio language, one track each'
		);
		expect(screen.getByTestId('jtp-default-disc-audio-eng')).toBeDisabled();
		expect(screen.getByTestId('jtp-save')).toBeDisabled();
		expect(fetchMock).toHaveBeenCalledWith('job_1', null);
	});

	it('saves a chosen selection seeded from the default, then reports it saved', async () => {
		fetchMock.mockResolvedValue(trackView());
		const saved = trackView({
			override: { ...trackView().default, audio_selection: 'languages', audio_languages: ['jpn'] }
		});
		saveMock.mockResolvedValue(saved);
		render();
		await waitFor(() => expect(screen.getByTestId('jtp-custom')).toBeInTheDocument());
		await fireEvent.click(screen.getByTestId('jtp-custom'));
		await fireEvent.click(screen.getByTestId('jtp-disc-audio-eng'));
		await fireEvent.click(screen.getByTestId('jtp-save'));
		await waitFor(() =>
			expect(saveMock).toHaveBeenCalledWith(
				'job_1',
				expect.objectContaining({
					audio_selection: 'languages',
					audio_languages: ['jpn'],
					audio_format: 'ac3_else_aac51'
				})
			)
		);
		await waitFor(() => expect(screen.getByTestId('jtp-saved')).toBeInTheDocument());
		expect((screen.getByTestId('jtp-custom') as HTMLInputElement).checked).toBe(true);
	});

	it('clears the override when the choice is switched off', async () => {
		fetchMock.mockResolvedValue(trackView({ override: { ...trackView().default, subtitle_selection: 'none' } }));
		saveMock.mockResolvedValue(trackView());
		render();
		await waitFor(() => expect((screen.getByTestId('jtp-custom') as HTMLInputElement).checked).toBe(true));
		expect((screen.getByTestId('jtp-subtitle-selection') as HTMLSelectElement).value).toBe('none');
		await fireEvent.click(screen.getByTestId('jtp-custom'));
		await fireEvent.click(screen.getByTestId('jtp-save'));
		await waitFor(() => expect(saveMock).toHaveBeenCalledWith('job_1', null));
	});

	it('blocks saving a languages choice with no language, and shows a save error', async () => {
		fetchMock.mockResolvedValue(trackView());
		saveMock.mockRejectedValue(new Error('server said no'));
		render();
		await waitFor(() => expect(screen.getByTestId('jtp-custom')).toBeInTheDocument());
		await fireEvent.click(screen.getByTestId('jtp-custom'));
		// "Only these languages" starts with an empty list.
		await fireEvent.change(screen.getByTestId('jtp-audio-selection'), { target: { value: 'languages' } });
		expect(screen.getByTestId('jtp-audio-missing')).toBeInTheDocument();
		expect(screen.getByTestId('jtp-save')).toBeDisabled();
		await fireEvent.change(screen.getByTestId('jtp-audio-selection'), { target: { value: 'all' } });
		await fireEvent.click(screen.getByTestId('jtp-save'));
		await waitFor(() => expect(screen.getByTestId('jtp-save-error')).toHaveTextContent('server said no'));
	});

	it('is read only once a transcode has started, and for a guest', async () => {
		fetchMock.mockResolvedValue(trackView({ editable: false, locked_reason: 'A transcode of this job has started.' }));
		render();
		await waitFor(() => expect(screen.getByTestId('jtp-locked')).toHaveTextContent('has started'));
		expect(screen.getByTestId('jtp-custom')).toBeDisabled();
		expect(screen.queryByTestId('jtp-save')).toBeNull();
		cleanup();
		(authStore as unknown as { __setRole: (r: string | null) => void }).__setRole('guest');
		fetchMock.mockResolvedValue(trackView());
		render();
		await waitFor(() => expect(screen.getByTestId('jtp-custom')).toBeDisabled());
	});

	it('says when the disc languages are unknown, and when loading fails', async () => {
		fetchMock.mockResolvedValue(trackView({ disc: { known: false, audio: [], subtitles: [] } }));
		render();
		await waitFor(() => expect(screen.getByTestId('jtp-disc-unknown')).toBeInTheDocument());
		expect(screen.queryByTestId('jtp-default-track-editor')).toBeNull();
		cleanup();
		fetchMock.mockRejectedValue(new Error('offline'));
		render();
		await waitFor(() => expect(screen.getByTestId('jtp-load-error')).toHaveTextContent('offline'));
	});

	it('apply: the default follows the chosen session, and a locked job stays editable', async () => {
		fetchMock.mockResolvedValue(trackView({ editable: false, locked_reason: 'started' }));
		const { rerender } = render({ variant: 'apply', sessionId: 'ses_a' });
		await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('job_1', 'ses_a'));
		await waitFor(() => expect(screen.getByTestId('jtp-custom')).not.toBeDisabled());
		expect(screen.queryByTestId('jtp-save')).toBeNull();
		await rerender({ jobId: 'job_1', variant: 'apply', sessionId: 'ses_b' });
		await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('job_1', 'ses_b'));
	});
});
