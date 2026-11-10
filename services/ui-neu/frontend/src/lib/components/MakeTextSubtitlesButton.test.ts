import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import MakeTextSubtitlesButton from './MakeTextSubtitlesButton.svelte';

vi.mock('$lib/api/jobs', () => ({ requestSubtitleOcr: vi.fn() }));

import { requestSubtitleOcr } from '$lib/api/jobs';

const requestMock = vi.mocked(requestSubtitleOcr);

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

const view = (eligible: number, existing: number) => ({
	override: null,
	default: true,
	effective: true,
	eligible_encodes: eligible,
	with_text_subtitles: existing
});

function render(eligible: number, existing: number, ondone = vi.fn()) {
	renderComponent(MakeTextSubtitlesButton, { props: { jobId: 'job_1', view: view(eligible, existing), ondone } });
	return ondone;
}

describe('MakeTextSubtitlesButton', () => {
	it('queues at once when no encode has text subtitles yet', async () => {
		requestMock.mockResolvedValue({ tasks: [], skipped: [] });
		const ondone = render(3, 0);
		await fireEvent.click(screen.getByTestId('mts-open'));
		await waitFor(() => expect(ondone).toHaveBeenCalledWith({ tasks: [], skipped: [] }));
		expect(requestMock).toHaveBeenCalledWith('job_1', false);
	});

	it('asks first when some have them: only the missing ones, or replace all', async () => {
		requestMock.mockResolvedValue({ tasks: [], skipped: [] });
		render(3, 1);
		await fireEvent.click(screen.getByTestId('mts-open'));
		expect(requestMock).not.toHaveBeenCalled();
		expect(screen.getByTestId('mts-question')).toHaveTextContent('1 of 3 already has text subtitles.');
		await fireEvent.click(screen.getByTestId('mts-missing'));
		await waitFor(() => expect(requestMock).toHaveBeenCalledWith('job_1', false));
		await fireEvent.click(screen.getByTestId('mts-open'));
		await fireEvent.click(screen.getByTestId('mts-replace'));
		await waitFor(() => expect(requestMock).toHaveBeenLastCalledWith('job_1', true));
	});

	it('offers only replace when every encode has them, and can cancel', async () => {
		render(2, 2);
		await fireEvent.click(screen.getByTestId('mts-open'));
		expect(screen.getByTestId('mts-question')).toHaveTextContent('2 of 2 already have text subtitles.');
		expect(screen.queryByTestId('mts-missing')).toBeNull();
		await fireEvent.click(screen.getByTestId('mts-cancel'));
		expect(screen.getByTestId('mts-open')).toBeInTheDocument();
		expect(requestMock).not.toHaveBeenCalled();
	});

	it('shows the error when queueing fails', async () => {
		requestMock.mockRejectedValue(new Error('text subtitles are off in Settings > Transcoding'));
		const ondone = render(1, 0);
		await fireEvent.click(screen.getByTestId('mts-open'));
		await waitFor(() =>
			expect(screen.getByTestId('mts-error')).toHaveTextContent('text subtitles are off in Settings > Transcoding')
		);
		expect(ondone).not.toHaveBeenCalled();
	});
});
