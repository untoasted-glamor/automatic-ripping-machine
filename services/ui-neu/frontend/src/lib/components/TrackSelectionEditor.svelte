<script lang="ts">
	// Which audio and subtitle tracks a transcode keeps, what the audio
	// becomes and where subtitles go. Used by the transcode preset form and by
	// a job's own picker (JobTrackPicker). Controlled: `value` in, `onchange`
	// out. With `disc`, it also lists the languages the disc's kept titles
	// have; ticking one adds it to (or removes it from) the language list.
	import LanguageListEditor from './LanguageListEditor.svelte';
	import { languageLabel } from '$lib/languages';
	import {
		AUDIO_FORMAT_HELP,
		AUDIO_FORMAT_LABELS,
		AUDIO_SELECTION_LABELS,
		SUBTITLE_OUTPUT_LABELS,
		SUBTITLE_SELECTION_LABELS,
		type TrackSettings
	} from '$lib/utils/track-selection';
	import type {
		AudioFormat,
		AudioSelection,
		ContainerFormat,
		DiscLanguageTracks,
		DiscTrackLanguages,
		SubtitleOutput,
		SubtitleSelection
	} from '$lib/types/api.gen';

	interface Props {
		value: TrackSettings;
		onchange: (v: TrackSettings) => void;
		/** data-testid prefix, e.g. `tp` gives `tp-audio-selection`. */
		prefix: string;
		disabled?: boolean;
		/** The ffmpeg VAAPI engine runs instead of HandBrake (preset-mode help differs). */
		usesFfmpeg?: boolean;
		container?: ContainerFormat | null;
		disc?: DiscTrackLanguages | null;
	}

	let {
		value,
		onchange,
		prefix,
		disabled = false,
		usesFfmpeg = false,
		container = null,
		disc = null
	}: Props = $props();

	function set(patch: Partial<TrackSettings>): void {
		onchange({ ...value, ...patch });
	}

	const AUDIO_SELECTIONS: AudioSelection[] = ['all', 'languages', 'preset'];
	const SUBTITLE_SELECTIONS: SubtitleSelection[] = ['all', 'languages', 'none', 'preset'];
	const SUBTITLE_OUTPUTS: SubtitleOutput[] = ['separate', 'embedded', 'both'];
	const AUDIO_FORMATS: AudioFormat[] = ['ac3_else_aac51', 'aac_stereo', 'aac_51', 'copy', 'preset'];

	let audioListMissing = $derived(value.audio_selection === 'languages' && value.audio_languages.length === 0);
	let subtitleListMissing = $derived(value.subtitle_selection === 'languages' && value.subtitle_languages.length === 0);
	let keepsSubtitles = $derived(value.subtitle_selection === 'all' || value.subtitle_selection === 'languages');

	let audioHelp = $derived(
		value.audio_selection === 'preset'
			? usesFfmpeg
				? 'Keeps every audio track, copied.'
				: 'Keeps what the HandBrake preset keeps: the first audio track only.'
			: value.audio_selection === 'all'
				? 'Keeps every audio language in disc order. The first one is the default track.'
				: 'Keeps the tracks in these languages, in this order. The first one is the default track. If none of them is on the disc, the first audio track is kept.'
	);
	let subtitleHelp = $derived(
		value.subtitle_selection === 'preset'
			? usesFfmpeg
				? 'Keeps every subtitle track in an MKV, none in an MP4.'
				: 'Keeps what the HandBrake preset keeps: no subtitles.'
			: value.subtitle_selection === 'none'
				? 'Keeps no subtitles.'
				: value.subtitle_selection === 'all'
					? 'Keeps every subtitle track.'
					: 'Keeps the subtitle tracks in these languages, in this order.'
	);

	const discKnown = $derived(disc?.known ?? false);
	const discAudio = $derived(new Set((disc?.audio ?? []).map((l) => l.language)));
	const discSubs = $derived(new Set((disc?.subtitles ?? []).map((l) => l.language)));
	const absentAudio = (code: string) => (discKnown && !discAudio.has(code) ? 'Not on this disc' : null);
	const absentSub = (code: string) => (discKnown && !discSubs.has(code) ? 'Not on this disc' : null);

	function audioKept(code: string): boolean {
		return (
			value.audio_selection === 'all' || (value.audio_selection === 'languages' && value.audio_languages.includes(code))
		);
	}
	function subtitleKept(code: string): boolean {
		return (
			value.subtitle_selection === 'all' ||
			(value.subtitle_selection === 'languages' && value.subtitle_languages.includes(code))
		);
	}

	// Ticking a disc language edits the language list; from "all", unticking
	// one turns the choice into the list of the others.
	function toggle(kind: 'audio' | 'subtitle', code: string, keep: boolean): void {
		const onDisc = (kind === 'audio' ? disc?.audio : disc?.subtitles) ?? [];
		const selection = kind === 'audio' ? value.audio_selection : value.subtitle_selection;
		const current = kind === 'audio' ? value.audio_languages : value.subtitle_languages;
		let list: string[];
		if (selection === 'all') list = onDisc.map((l) => l.language);
		else if (selection === 'languages') list = [...current];
		else list = [];
		list = keep ? (list.includes(code) ? list : [...list, code]) : list.filter((c) => c !== code);
		if (kind === 'audio') set({ audio_selection: 'languages', audio_languages: list });
		else set({ subtitle_selection: 'languages', subtitle_languages: list });
	}

	function trackLine(lang: DiscLanguageTracks): string {
		const tracks = (lang.tracks ?? []).join(', ');
		const titles = lang.title_count && lang.title_count > 1 ? ` (${lang.title_count} titles)` : '';
		const forced = lang.forced_only ? 'forced only: ' : '';
		return `${forced}${tracks}${titles}`;
	}
</script>

<div class="stack track-selection-editor" data-testid="{prefix}-track-editor">
	{#if discKnown}
		<div class="track-selection-disc" data-testid="{prefix}-disc">
			<p class="field-label">On this disc</p>
			{#each [{ kind: 'audio' as const, label: 'Audio', langs: disc?.audio ?? [] }, { kind: 'subtitle' as const, label: 'Subtitles', langs: disc?.subtitles ?? [] }] as group (group.kind)}
				<div class="track-selection-disc-group">
					<span class="track-selection-disc-kind">{group.label}</span>
					{#if group.langs.length === 0}
						<span class="field-help" data-testid="{prefix}-disc-{group.kind}-none">None</span>
					{:else}
						<ul class="track-selection-disc-list">
							{#each group.langs as lang (lang.language)}
								<li>
									<label class="field-row track-selection-disc-row">
										<input
											type="checkbox"
											data-testid="{prefix}-disc-{group.kind}-{lang.language}"
											checked={group.kind === 'audio' ? audioKept(lang.language) : subtitleKept(lang.language)}
											{disabled}
											onchange={(e) => toggle(group.kind, lang.language, e.currentTarget.checked)}
										/>
										<span class="track-selection-disc-name">{languageLabel(lang.language)}</span>
										<span class="field-help">{trackLine(lang)}</span>
									</label>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/each}
		</div>
	{/if}

	<label class="field">
		<span class="field-label">Audio tracks</span>
		<select
			data-testid="{prefix}-audio-selection"
			value={value.audio_selection}
			{disabled}
			onchange={(e) => set({ audio_selection: e.currentTarget.value as AudioSelection })}
		>
			{#each AUDIO_SELECTIONS as option (option)}
				<option value={option}>{AUDIO_SELECTION_LABELS[option]}</option>
			{/each}
		</select>
		<p class="field-help" data-testid="{prefix}-audio-help">{audioHelp}</p>
	</label>
	{#if value.audio_selection === 'languages'}
		<LanguageListEditor
			value={value.audio_languages}
			onchange={(v) => set({ audio_languages: v })}
			label="Audio languages"
			testid="{prefix}-audio-languages"
			firstNote="default track"
			itemNote={absentAudio}
			{disabled}
		/>
		{#if audioListMissing}
			<p class="field-error" data-testid="{prefix}-audio-missing">Add at least one audio language.</p>
		{/if}
	{/if}
	{#if value.audio_selection !== 'preset'}
		<label class="field">
			<span class="field-label">Audio format</span>
			<select
				data-testid="{prefix}-audio-format"
				value={value.audio_format}
				{disabled}
				onchange={(e) => set({ audio_format: e.currentTarget.value as AudioFormat })}
			>
				{#each AUDIO_FORMATS as option (option)}
					<option value={option}>{AUDIO_FORMAT_LABELS[option]}</option>
				{/each}
			</select>
			<p class="field-help" data-testid="{prefix}-audio-format-help">{AUDIO_FORMAT_HELP[value.audio_format]}</p>
		</label>
	{/if}

	<label class="field">
		<span class="field-label">Subtitle tracks</span>
		<select
			data-testid="{prefix}-subtitle-selection"
			value={value.subtitle_selection}
			{disabled}
			onchange={(e) => set({ subtitle_selection: e.currentTarget.value as SubtitleSelection })}
		>
			{#each SUBTITLE_SELECTIONS as option (option)}
				<option value={option}>{SUBTITLE_SELECTION_LABELS[option]}</option>
			{/each}
		</select>
		<p class="field-help" data-testid="{prefix}-subtitle-help">{subtitleHelp}</p>
	</label>
	{#if value.subtitle_selection === 'languages'}
		<LanguageListEditor
			value={value.subtitle_languages}
			onchange={(v) => set({ subtitle_languages: v })}
			label="Subtitle languages"
			testid="{prefix}-subtitle-languages"
			itemNote={absentSub}
			{disabled}
		/>
		{#if subtitleListMissing}
			<p class="field-error" data-testid="{prefix}-subtitle-missing">Add at least one subtitle language.</p>
		{/if}
	{/if}

	{#if keepsSubtitles}
		<label class="field">
			<span class="field-label">Subtitles go</span>
			<select
				data-testid="{prefix}-subtitle-output"
				value={value.subtitle_output}
				{disabled}
				onchange={(e) => set({ subtitle_output: e.currentTarget.value as SubtitleOutput })}
			>
				{#each SUBTITLE_OUTPUTS as option (option)}
					<option value={option}>{SUBTITLE_OUTPUT_LABELS[option]}</option>
				{/each}
			</select>
			{#if value.subtitle_output !== 'embedded'}
				<p class="field-help" data-testid="{prefix}-subtitle-files-note">
					Subtitle files keep the disc's own format (.sup for Blu-ray, .mks for DVD) and are named for Jellyfin, for
					example Movie.eng.sup or Movie.eng.forced.sup.
				</p>
			{/if}
			{#if value.subtitle_output !== 'separate' && container === 'mp4'}
				<p class="field-help" data-testid="{prefix}-subtitle-mp4-note">
					MP4 cannot hold Blu-ray or DVD picture subtitles; only text subtitles go inside the file.
				</p>
			{/if}
		</label>
	{/if}
</div>

<style>
	.track-selection-disc {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		padding: 0.5rem 0.75rem;
	}
	.track-selection-disc-group {
		display: grid;
		grid-template-columns: 5.5rem minmax(0, 1fr);
		gap: 0.5rem;
		align-items: start;
	}
	.track-selection-disc-kind {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--color-text-secondary);
		padding-top: 0.125rem;
	}
	.track-selection-disc-list {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.track-selection-disc-row {
		align-items: baseline;
		flex-wrap: wrap;
	}
	.track-selection-disc-name {
		font-size: 0.875rem;
		color: var(--color-text);
	}
</style>
