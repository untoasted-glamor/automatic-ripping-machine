<script lang="ts">
	// Ported from the removed Vue UI's TranscodePresetForm view, structured to
	// match the sibling RipPresetForm.svelte (T2a). Inline (no-route) form
	// driven by props. media_type is immutable on edit; built-in presets are
	// name-only; nullable fields submit `value || null`. `encoder` is one
	// arm_common.encoders catalog id. preset_json is not exposed. The track
	// fields pick the audio and subtitle tracks a HandBrake-tool preset keeps.
	import { onMount } from 'svelte';
	import { createTranscodePreset, updateTranscodePreset } from '$lib/api/transcodePresets';
	import { encodersStore, PRESET_ENCODER_ID } from '$lib/stores/encoders.svelte';
	import LanguageListEditor from './LanguageListEditor.svelte';
	import type {
		AudioSelection,
		ContainerFormat,
		EncoderAvailabilityView,
		MediaType,
		SubtitleOutput,
		SubtitleSelection,
		TranscodePresetView,
		TranscodeTool
	} from '$lib/types/api.gen';

	// Encoder optgroup order and headings; a group is only rendered when the
	// catalog response has an entry for it.
	const GROUP_ORDER: EncoderAvailabilityView['group'][] = ['preset', 'cpu', 'any', 'qsv', 'nvenc', 'vaapi'];
	const GROUP_LABELS: Record<EncoderAvailabilityView['group'], string> = {
		preset: "HandBrake preset's own",
		cpu: 'CPU',
		any: 'Any GPU',
		qsv: 'Intel QSV',
		nvenc: 'NVIDIA NVENC',
		vaapi: 'AMD VAAPI'
	};

	// A HandBrake preset name that names a hardware encoder (a common
	// convention in preset libraries) does nothing on its own. HandBrake
	// only attaches a GPU when the Encoder field above asks for one.
	const HARDWARE_HINT_PATTERN = /QSV|NVENC|VCN|VCE/i;

	let {
		preset = null,
		onsaved,
		oncancel
	}: {
		preset?: TranscodePresetView | null;
		onsaved: (p: TranscodePresetView) => void;
		oncancel: () => void;
	} = $props();

	const editing = $derived(!!preset);
	const isBuiltin = $derived(preset?.is_builtin ?? false);

	let name = $state(preset?.name ?? '');
	let mediaType = $state<MediaType>(preset?.media_type ?? 'movie');
	let tool = $state<TranscodeTool>(preset?.tool ?? 'handbrake');
	let presetRef = $state(preset?.preset_ref ?? '');
	let container = $state<ContainerFormat>(preset?.container ?? 'mkv');
	let encoder = $state<string>(preset?.encoder ?? PRESET_ENCODER_ID);

	// Availability-aware encoder catalog (GET /api/encoders, cached by the
	// shared store), in catalog order. Refreshed on open: availability moves
	// with GPU probes and the enabled switches.
	onMount(() => {
		encodersStore.refresh();
	});
	let encoders = $derived(encodersStore.list);
	let encodersLoading = $derived(encodersStore.loading);
	let encodersError = $derived(encodersStore.error);

	let groupedEncoders = $derived(
		(() => {
			// eslint-disable-next-line svelte/prefer-svelte-reactivity -- built fresh inside $derived and never mutated afterwards
			const groups = new Map<string, EncoderAvailabilityView[]>();
			for (const enc of encoders) {
				const list = groups.get(enc.group);
				if (list) list.push(enc);
				else groups.set(enc.group, [enc]);
			}
			return groups;
		})()
	);

	// abcde/none presets can't carry a codec encoder (only handbrake presets
	// can); force it back to the tool's own encoder so saving never 422s.
	let encoderLocked = $derived(tool === 'abcde' || tool === 'none');
	$effect(() => {
		if (encoderLocked && !isBuiltin) encoder = PRESET_ENCODER_ID;
	});

	let selectedEncoder = $derived(encoders.find((e) => e.id === encoder));
	// vaapi_* encoders run over ffmpeg directly; the HandBrake preset name
	// plays no part, and extra_args are ffmpeg CLI flags, not HandBrake ones.
	let usesFfmpegVaapi = $derived(selectedEncoder?.engine === 'ffmpeg_vaapi');
	// That ffmpeg path copies the source audio, which WebM cannot carry, so
	// it writes MKV or MP4 only (the API refuses the pairing with a 422).
	$effect(() => {
		if (usesFfmpegVaapi && container === 'webm' && !isBuiltin) container = 'mkv';
	});
	// An any_* encoder may resolve to an AMD device at claim time, which runs
	// ffmpeg instead of HandBrake.
	let anyGpuSelected = $derived(selectedEncoder?.kind === 'any');

	let encoderHint = $derived(
		encoder === PRESET_ENCODER_ID && HARDWARE_HINT_PATTERN.test(presetRef)
			? 'This HandBrake preset uses a hardware encoder; choose the matching encoder above or ARM will not attach a GPU.'
			: null
	);

	let extraArgs = $state(preset?.extra_args ?? '');

	// Track selection. A new preset keeps every audio track and writes every
	// subtitle as a separate file (the API's defaults too).
	let audioSelection = $state<AudioSelection>(preset?.audio_selection ?? 'all');
	let audioLanguages = $state<string[]>(preset?.audio_languages ?? []);
	let subtitleSelection = $state<SubtitleSelection>(preset?.subtitle_selection ?? 'all');
	let subtitleLanguages = $state<string[]>(preset?.subtitle_languages ?? []);
	let subtitleOutput = $state<SubtitleOutput>(preset?.subtitle_output ?? 'separate');

	let audioListMissing = $derived(audioSelection === 'languages' && audioLanguages.length === 0);
	let subtitleListMissing = $derived(subtitleSelection === 'languages' && subtitleLanguages.length === 0);
	let keepsSubtitles = $derived(subtitleSelection === 'all' || subtitleSelection === 'languages');

	let audioHelp = $derived(
		audioSelection === 'preset'
			? usesFfmpegVaapi
				? 'Keeps every audio track.'
				: 'Keeps what the HandBrake preset keeps: the first audio track only.'
			: audioSelection === 'all'
				? 'Keeps every audio track in disc order. The first one is the default track.'
				: 'Keeps the tracks in these languages, in this order. The first one is the default track. If none of them is on the disc, the first audio track is kept.'
	);
	let subtitleHelp = $derived(
		subtitleSelection === 'preset'
			? usesFfmpegVaapi
				? 'Keeps every subtitle track in an MKV, none in an MP4.'
				: 'Keeps what the HandBrake preset keeps: no subtitles.'
			: subtitleSelection === 'none'
				? 'Keeps no subtitles.'
				: subtitleSelection === 'all'
					? 'Keeps every subtitle track.'
					: 'Keeps the subtitle tracks in these languages, in this order.'
	);

	let submitting = $state(false);
	let error = $state<string | null>(null);

	const canSubmit = $derived(!submitting && name.trim().length > 0 && !audioListMissing && !subtitleListMissing);

	function trackFields() {
		return {
			audio_selection: audioSelection,
			audio_languages: audioLanguages,
			subtitle_selection: subtitleSelection,
			subtitle_languages: subtitleLanguages,
			subtitle_output: subtitleOutput
		};
	}

	async function submit(event: Event): Promise<void> {
		event.preventDefault();
		// Built-ins are read-only — no save path (Enter is a no-op too).
		if (isBuiltin || !canSubmit) return;
		submitting = true;
		error = null;
		try {
			let result: TranscodePresetView;
			if (editing && preset) {
				// Custom edit: no media_type (immutable after create).
				result = await updateTranscodePreset(preset.id, {
					name,
					tool,
					preset_ref: presetRef || null,
					container,
					encoder,
					extra_args: extraArgs || null,
					...trackFields()
				});
			} else {
				result = await createTranscodePreset({
					name,
					media_type: mediaType,
					tool,
					preset_ref: presetRef || null,
					container,
					encoder,
					extra_args: extraArgs || null,
					...trackFields()
				});
			}
			onsaved(result);
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			submitting = false;
		}
	}

	// the old shared input-class constants are gone: inputs live in .field
	// wrappers now, styled by field.css's descendant rule.
</script>

<form class="stack transcode-preset-form" onsubmit={submit}>
	<h3 class="transcode-preset-form-title">
		{isBuiltin ? 'View transcode preset' : editing ? 'Edit transcode preset' : 'New transcode preset'}
	</h3>

	{#if isBuiltin}
		<div class="alert alert-warning" data-testid="tp-builtin-note" role="status">
			This is a built-in preset and can't be edited. Clone it to make an editable copy.
		</div>
	{/if}

	{#if error}
		<p class="field-error" data-testid="tp-error">{error}</p>
	{/if}

	<label class="field">
		<span class="field-label">Name</span>
		<input
			id="tp-name"
			data-testid="tp-name"
			type="text"
			required
			bind:value={name}
			disabled={submitting || isBuiltin}
		/>
	</label>

	<label class="field">
		<span class="field-label">Media type</span>
		<select id="tp-media-type" data-testid="tp-media-type" bind:value={mediaType} disabled={editing}>
			<option value="movie">Movie</option>
			<option value="tv">TV</option>
			<option value="music">Music</option>
			<option value="data">Data</option>
			<option value="iso">ISO</option>
		</select>
	</label>

	<label class="field">
		<span class="field-label">Tool</span>
		<select id="tp-tool" data-testid="tp-tool" bind:value={tool} disabled={isBuiltin}>
			<option value="handbrake">HandBrake</option>
			<option value="abcde">abcde</option>
			<option value="none">None</option>
		</select>
	</label>

	<label class="field">
		<span class="field-label">Preset ref (HandBrake/abcde profile name)</span>
		<input id="tp-preset-ref" data-testid="tp-preset-ref" type="text" bind:value={presetRef} disabled={isBuiltin} />
		{#if usesFfmpegVaapi}
			<p class="field-help" data-testid="tp-preset-ref-note">Not used by this encoder.</p>
		{/if}
	</label>

	<label class="field">
		<span class="field-label">Container</span>
		<select id="tp-container" data-testid="tp-container" bind:value={container} disabled={isBuiltin}>
			<option value="mkv">MKV</option>
			<option value="mp4">MP4</option>
			<option value="webm" disabled={usesFfmpegVaapi}>WebM</option>
			<option value="flac">FLAC</option>
			<option value="mp3">MP3</option>
			<option value="ogg">OGG</option>
			<option value="iso">ISO</option>
			<option value="none">None</option>
		</select>
		{#if usesFfmpegVaapi}
			<p class="field-help" data-testid="tp-container-vaapi-note">AMD VAAPI encoders write MKV or MP4.</p>
		{/if}
	</label>

	<label class="field">
		<span class="field-label">Encoder</span>
		<select
			id="tp-encoder"
			data-testid="tp-encoder"
			bind:value={encoder}
			disabled={isBuiltin || encoderLocked || encodersLoading}
		>
			{#if encodersLoading}
				<option value={encoder} disabled>Loading encoders...</option>
			{:else if encoders.length === 0}
				<option value={encoder}>{encoder}</option>
			{:else}
				{#each GROUP_ORDER as g (g)}
					{#if groupedEncoders.get(g)?.length}
						<optgroup label={GROUP_LABELS[g]}>
							{#each groupedEncoders.get(g) ?? [] as enc (enc.id)}
								<option value={enc.id} disabled={!enc.available} title={enc.reason ?? undefined}>
									{enc.label}{enc.reason ? ` (${enc.reason})` : ''}
								</option>
							{/each}
						</optgroup>
					{/if}
				{/each}
			{/if}
		</select>
		{#if encodersError}
			<p class="field-error" data-testid="tp-encoder-error">Could not load encoders; the current encoder is kept.</p>
		{/if}
		{#if anyGpuSelected}
			<p class="field-help" data-testid="tp-encoder-any-note">
				HandBrake preset settings (scaling, filters, audio) apply on CPU, NVENC and QSV, but not when the job runs on an
				AMD (VAAPI) device.
			</p>
		{/if}
		{#if encoderHint}
			<p class="field-help" data-testid="tp-encoder-hint">{encoderHint}</p>
		{/if}
	</label>

	<label class="field">
		<span class="field-label">{usesFfmpegVaapi ? 'ffmpeg arguments' : 'Extra args'}</span>
		<input id="tp-extra-args" data-testid="tp-extra-args" type="text" bind:value={extraArgs} disabled={isBuiltin} />
	</label>

	{#if tool === 'handbrake'}
		<fieldset class="stack transcode-preset-form-tracks" data-testid="tp-tracks">
			<legend class="field-label">Tracks</legend>

			<label class="field">
				<span class="field-label">Audio tracks</span>
				<select data-testid="tp-audio-selection" bind:value={audioSelection} disabled={isBuiltin}>
					<option value="preset">As the preset does</option>
					<option value="all">All tracks</option>
					<option value="languages">Only these languages</option>
				</select>
				<p class="field-help" data-testid="tp-audio-help">{audioHelp}</p>
			</label>
			{#if audioSelection === 'languages'}
				<LanguageListEditor
					value={audioLanguages}
					onchange={(v) => (audioLanguages = v)}
					label="Audio languages"
					testid="tp-audio-languages"
					firstNote="default track"
					disabled={isBuiltin}
				/>
				{#if audioListMissing}
					<p class="field-error" data-testid="tp-audio-missing">Add at least one audio language.</p>
				{/if}
			{/if}

			<label class="field">
				<span class="field-label">Subtitle tracks</span>
				<select data-testid="tp-subtitle-selection" bind:value={subtitleSelection} disabled={isBuiltin}>
					<option value="preset">As the preset does</option>
					<option value="all">All tracks</option>
					<option value="languages">Only these languages</option>
					<option value="none">None</option>
				</select>
				<p class="field-help" data-testid="tp-subtitle-help">{subtitleHelp}</p>
			</label>
			{#if subtitleSelection === 'languages'}
				<LanguageListEditor
					value={subtitleLanguages}
					onchange={(v) => (subtitleLanguages = v)}
					label="Subtitle languages"
					testid="tp-subtitle-languages"
					disabled={isBuiltin}
				/>
				{#if subtitleListMissing}
					<p class="field-error" data-testid="tp-subtitle-missing">Add at least one subtitle language.</p>
				{/if}
			{/if}

			{#if keepsSubtitles}
				<label class="field">
					<span class="field-label">Subtitles go</span>
					<select data-testid="tp-subtitle-output" bind:value={subtitleOutput} disabled={isBuiltin}>
						<option value="separate">Separate files next to the video</option>
						<option value="embedded">Inside the video file</option>
						<option value="both">Both</option>
					</select>
					{#if subtitleOutput !== 'embedded'}
						<p class="field-help" data-testid="tp-subtitle-files-note">
							Subtitle files keep the disc's own format (.sup for Blu-ray, .mks for DVD) and are named for Jellyfin, for
							example Movie.eng.sup or Movie.eng.forced.sup.
						</p>
					{/if}
					{#if subtitleOutput !== 'separate' && container === 'mp4'}
						<p class="field-help" data-testid="tp-subtitle-mp4-note">
							MP4 cannot hold Blu-ray or DVD picture subtitles; only text subtitles go inside the file.
						</p>
					{/if}
				</label>
			{/if}
		</fieldset>
	{/if}

	<div class="transcode-preset-form-actions">
		<button type="button" onclick={oncancel} disabled={submitting} class="btn btn-ghost">
			{isBuiltin ? 'Close' : 'Cancel'}
		</button>
		{#if !isBuiltin}
			<button type="submit" disabled={!canSubmit} data-testid="tp-submit" class="btn btn-primary">
				{submitting ? 'Saving...' : 'Save'}
			</button>
		{/if}
	</div>
</form>

<style>
	.transcode-preset-form-title {
		font-size: 1.125rem;
		line-height: 1.75rem;
		font-weight: 600;
		color: var(--color-text);
	}
	.transcode-preset-form-tracks {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		padding: 0.75rem 1rem 1rem;
	}
	.transcode-preset-form-tracks legend {
		padding: 0 0.25rem;
	}
	.transcode-preset-form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.75rem;
		padding-top: 0.5rem;
	}
</style>
