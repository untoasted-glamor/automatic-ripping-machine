import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup } from '$lib/test-utils';
import TrackSelectionEditor from '../TrackSelectionEditor.svelte';
import {
	DEFAULT_TRACKS,
	describeTracks,
	fullTracks,
	tracksValid,
	type TrackSettings
} from '$lib/utils/track-selection';
import { trackView } from '../__fixtures__/trackSelection';

function render(value: Partial<TrackSettings> = {}, extra: Record<string, unknown> = {}) {
	const onchange = vi.fn();
	renderComponent(TrackSelectionEditor, {
		props: { value: { ...DEFAULT_TRACKS, ...value }, onchange, prefix: 'te', disc: trackView().disc, ...extra }
	});
	return onchange;
}

describe('TrackSelectionEditor', () => {
	afterEach(() => cleanup());

	it('lists the disc languages with their tracks, all ticked for "all"', () => {
		render();
		const eng = screen.getByTestId('te-disc-audio-eng') as HTMLInputElement;
		expect(eng.checked).toBe(true);
		expect(screen.getByTestId('te-disc')).toHaveTextContent(
			'TrueHD Surround 5.1 English, DD Surround 5.1 English (7 titles)'
		);
		expect((screen.getByTestId('te-disc-subtitle-eng') as HTMLInputElement).checked).toBe(true);
	});

	it('unticking a language from "all" keeps the others as a list', async () => {
		const onchange = render();
		await fireEvent.click(screen.getByTestId('te-disc-audio-eng'));
		expect(onchange).toHaveBeenLastCalledWith(
			expect.objectContaining({ audio_selection: 'languages', audio_languages: ['jpn'] })
		);
	});

	it('ticking adds to the list, and from none starts one', async () => {
		const onchange = render({ audio_selection: 'languages', audio_languages: ['jpn'], subtitle_selection: 'none' });
		expect((screen.getByTestId('te-disc-audio-eng') as HTMLInputElement).checked).toBe(false);
		await fireEvent.click(screen.getByTestId('te-disc-audio-eng'));
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ audio_languages: ['jpn', 'eng'] }));
		await fireEvent.click(screen.getByTestId('te-disc-subtitle-eng'));
		expect(onchange).toHaveBeenLastCalledWith(
			expect.objectContaining({ subtitle_selection: 'languages', subtitle_languages: ['eng'] })
		);
	});

	it('unticking a listed language removes it', async () => {
		const onchange = render({ audio_selection: 'languages', audio_languages: ['jpn', 'eng'] });
		await fireEvent.click(screen.getByTestId('te-disc-audio-jpn'));
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ audio_languages: ['eng'] }));
	});

	it('marks a listed language the disc does not have, and does not drop it', () => {
		render({ subtitle_selection: 'languages', subtitle_languages: ['eng', 'fre'] });
		const items = screen.getAllByTestId('te-subtitle-languages-item');
		expect(items[1]).toHaveTextContent('fre | Not on this disc');
		expect(items[0]).not.toHaveTextContent('Not on this disc');
	});

	it('shows no disc block for an unknown disc, or a disc with no subtitles', () => {
		render({}, { disc: { known: false, audio: [], subtitles: [] } });
		expect(screen.queryByTestId('te-disc')).toBeNull();
		cleanup();
		render({}, { disc: { ...trackView().disc, subtitles: [] } });
		expect(screen.getByTestId('te-disc-subtitle-none')).toHaveTextContent('None');
	});

	it('picks the audio format, with help, and hides it when the preset picks the audio', async () => {
		const onchange = render();
		expect(screen.getByTestId('te-audio-format-help')).toHaveTextContent("the disc's Dolby Digital (AC-3) track");
		await fireEvent.change(screen.getByTestId('te-audio-format'), { target: { value: 'copy' } });
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ audio_format: 'copy' }));
		cleanup();
		render({ audio_selection: 'preset' });
		expect(screen.queryByTestId('te-audio-format')).toBeNull();
	});

	it('changes the selections and the subtitle output', async () => {
		const onchange = render({ audio_selection: 'languages', audio_languages: ['eng'] }, { container: 'mp4' });
		await fireEvent.change(screen.getByTestId('te-audio-selection'), { target: { value: 'all' } });
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ audio_selection: 'all' }));
		await fireEvent.change(screen.getByTestId('te-subtitle-selection'), { target: { value: 'none' } });
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ subtitle_selection: 'none' }));
		await fireEvent.change(screen.getByTestId('te-subtitle-output'), { target: { value: 'both' } });
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ subtitle_output: 'both' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove English' }));
		expect(onchange).toHaveBeenLastCalledWith(expect.objectContaining({ audio_languages: [] }));
	});

	it('shows the mp4 note when subtitles go inside an MP4', () => {
		render({ subtitle_output: 'both' }, { container: 'mp4' });
		expect(screen.getByTestId('te-subtitle-mp4-note')).toBeInTheDocument();
	});
});

describe('track-selection helpers', () => {
	it('describes the built-in default and other choices', () => {
		expect(describeTracks(DEFAULT_TRACKS)).toBe(
			'Every audio language, one track each: AC-3 copy, else AAC 5.1; every subtitle track as separate files.'
		);
		expect(
			describeTracks({
				...DEFAULT_TRACKS,
				audio_selection: 'languages',
				audio_languages: ['jpn', 'eng'],
				audio_format: 'aac_stereo',
				subtitle_selection: 'languages',
				subtitle_languages: ['eng'],
				subtitle_output: 'both'
			})
		).toBe('Audio in Japanese, English, AAC stereo; subtitles in English inside the video and as separate files.');
		expect(describeTracks({ ...DEFAULT_TRACKS, audio_selection: 'preset', subtitle_selection: 'preset' })).toBe(
			'Audio as the preset does; subtitles as the preset does.'
		);
		expect(describeTracks({ ...DEFAULT_TRACKS, subtitle_selection: 'none' })).toMatch(/; no subtitles\.$/);
		expect(describeTracks({ ...DEFAULT_TRACKS, subtitle_output: 'embedded' })).toMatch(/inside the video\.$/);
	});

	it('validates and fills values', () => {
		expect(tracksValid(DEFAULT_TRACKS)).toBe(true);
		expect(tracksValid({ ...DEFAULT_TRACKS, audio_selection: 'languages' })).toBe(false);
		expect(tracksValid({ ...DEFAULT_TRACKS, subtitle_selection: 'languages' })).toBe(false);
		expect(fullTracks(null)).toEqual(DEFAULT_TRACKS);
		expect(fullTracks({ audio_format: 'copy' }).audio_format).toBe('copy');
	});
});
