import type { JobTrackSelectionView } from '$lib/types/api.gen';

/** A JobTrackSelectionView for the Hanako-kun Blu-ray: eng + jpn audio, eng subtitles. */
export function trackView(overrides: Partial<JobTrackSelectionView> = {}): JobTrackSelectionView {
	const builtin = {
		audio_selection: 'all' as const,
		audio_languages: [],
		subtitle_selection: 'all' as const,
		subtitle_languages: [],
		subtitle_output: 'separate' as const,
		audio_format: 'ac3_else_aac51' as const
	};
	return {
		override: null,
		default: builtin,
		default_source: 'Plex TV 1080p H.265',
		default_preset_id: 'tpr_builtin_tv_plex_1080p_h265',
		effective: builtin,
		disc: {
			known: true,
			audio: [
				{
					language: 'eng',
					language_name: 'English',
					tracks: ['TrueHD Surround 5.1 English', 'DD Surround 5.1 English'],
					title_count: 7,
					forced_only: false
				},
				{
					language: 'jpn',
					language_name: 'Japanese',
					tracks: ['TrueHD Stereo Japanese', 'DD Stereo Japanese'],
					title_count: 7,
					forced_only: false
				}
			],
			subtitles: [
				{ language: 'eng', language_name: 'English', tracks: ['PGS English'], title_count: 7, forced_only: false }
			]
		},
		editable: true,
		locked_reason: null,
		...overrides
	};
}
