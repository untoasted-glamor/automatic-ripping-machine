// Track selection (the audio/subtitle fields of a transcode preset and a
// job's own override): option labels, the built-in default, and a one-line
// summary. Shared by TrackSelectionEditor and JobTrackPicker.
import type {
	AudioFormat,
	AudioSelection,
	SubtitleOutput,
	SubtitleSelection,
	TrackSelectionSettings
} from '$lib/types/api.gen';
import { languageLabel } from '$lib/languages';

export type TrackSettings = Required<TrackSelectionSettings>;

/** The built-in default (arm_common.schemas.TrackSelectionSettings). */
export const DEFAULT_TRACKS: TrackSettings = {
	audio_selection: 'all',
	audio_languages: [],
	subtitle_selection: 'all',
	subtitle_languages: [],
	subtitle_output: 'separate',
	audio_format: 'ac3_else_aac51'
};

/** Fill the fields an API value may leave out with the built-in default. */
export function fullTracks(value: TrackSelectionSettings | null | undefined): TrackSettings {
	return { ...DEFAULT_TRACKS, ...(value ?? {}) };
}

export const AUDIO_SELECTION_LABELS: Record<AudioSelection, string> = {
	preset: 'As the preset does',
	all: 'All languages',
	languages: 'Only these languages'
};

export const SUBTITLE_SELECTION_LABELS: Record<SubtitleSelection, string> = {
	preset: 'As the preset does',
	all: 'All tracks',
	languages: 'Only these languages',
	none: 'None'
};

export const SUBTITLE_OUTPUT_LABELS: Record<SubtitleOutput, string> = {
	separate: 'Separate files next to the video',
	embedded: 'Inside the video file',
	both: 'Both'
};

export const AUDIO_FORMAT_LABELS: Record<AudioFormat, string> = {
	ac3_else_aac51: 'One track per language: AC-3 copy, else AAC 5.1',
	aac_stereo: 'AAC stereo',
	aac_51: 'AAC 5.1',
	copy: 'Copy every track untouched',
	preset: "The HandBrake preset's audio settings"
};

export const AUDIO_FORMAT_HELP: Record<AudioFormat, string> = {
	ac3_else_aac51:
		"Keeps one track per language: the disc's Dolby Digital (AC-3) track, copied untouched. A language with no AC-3 track gets its best track converted to AAC with up to 5.1 channels; a stereo or mono track stays so. TrueHD and other duplicate tracks are dropped. Commentary tracks come after the main tracks.",
	aac_stereo: 'Converts every kept track to AAC stereo. Surround sound is mixed down.',
	aac_51: 'Converts every kept track to AAC with up to 5.1 channels; a stereo or mono track stays so.',
	copy: 'Keeps every kept track exactly as it is on the disc, TrueHD and DTS-HD included. Some players cannot play those.',
	preset: "Uses the HandBrake preset's own audio encoders. The built-in presets convert every track to AAC stereo."
};

function languages(codes: string[]): string {
	return codes.map((c) => languageLabel(c)).join(', ');
}

/** One plain sentence describing `value`. */
export function describeTracks(value: TrackSettings): string {
	const audio =
		value.audio_selection === 'preset'
			? 'audio as the preset does'
			: `${value.audio_selection === 'all' ? 'every audio language' : `audio in ${languages(value.audio_languages)}`}, ${AUDIO_FORMAT_LABELS[value.audio_format].replace(/^One track per language: /, 'one track each: ')}`;
	let subtitles: string;
	if (value.subtitle_selection === 'preset') subtitles = 'subtitles as the preset does';
	else if (value.subtitle_selection === 'none') subtitles = 'no subtitles';
	else {
		const which =
			value.subtitle_selection === 'all'
				? 'every subtitle track'
				: `subtitles in ${languages(value.subtitle_languages)}`;
		const where =
			value.subtitle_output === 'separate'
				? 'as separate files'
				: value.subtitle_output === 'embedded'
					? 'inside the video'
					: 'inside the video and as separate files';
		subtitles = `${which} ${where}`;
	}
	const text = `${audio}; ${subtitles}.`;
	return text.charAt(0).toUpperCase() + text.slice(1);
}

/** True when the value can be saved (a languages choice needs a language). */
export function tracksValid(value: TrackSettings): boolean {
	return !(
		(value.audio_selection === 'languages' && value.audio_languages.length === 0) ||
		(value.subtitle_selection === 'languages' && value.subtitle_languages.length === 0)
	);
}
