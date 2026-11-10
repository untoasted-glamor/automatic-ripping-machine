<script lang="ts">
	// A job's own audio/subtitle track choice (jobs.track_selection). Loads
	// the job's override, the default it would get (the transcode preset of
	// `sessionId`, else the job's session, else the built-in default) and the
	// languages on the disc's kept titles; "Choose tracks for this disc"
	// switches from that default to the job's own choice, seeded from it.
	// While text subtitles are on in Settings it also holds the job's text
	// subtitles choice (jobs.subtitle_ocr), offered when the subtitles go to
	// separate files (that is where the image subtitles it reads come from).
	//
	// variant:
	//  - `page`: its own Save button (job page). Read only once a transcode
	//    of the job has started.
	//  - `review`: no button; the caller saves with `save()` before Start.
	//  - `apply`: no button; the caller sends `value()` with Apply session.
	import { onMount } from 'svelte';
	import { fetchTrackSelection, saveTrackSelection } from '$lib/api/jobs';
	import { isAdmin } from '$lib/stores/auth';
	import TrackSelectionEditor from './TrackSelectionEditor.svelte';
	import { describeTracks, fullTracks, tracksValid, type TrackSettings } from '$lib/utils/track-selection';
	import type { JobTrackSelectionView } from '$lib/types/api.gen';

	interface Props {
		jobId: string;
		variant: 'page' | 'review' | 'apply';
		/** Apply session: the session being applied (its preset is the default). */
		sessionId?: string | null;
	}

	let { jobId, variant, sessionId = null }: Props = $props();

	let view = $state<JobTrackSelectionView | null>(null);
	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let custom = $state(false);
	let draft = $state<TrackSettings>(fullTracks(null));
	let dirty = $state(false);
	let saving = $state(false);
	let saveError = $state<string | null>(null);
	let saved = $state(false);
	// The job's text subtitles choice: its own value, else the Settings default.
	let ocrOverride = $state<boolean | null>(null);
	let ocrDirty = $state(false);

	const locked = $derived(view !== null && !view.editable && variant !== 'apply');
	const editable = $derived($isAdmin && !locked);
	const defaultTracks = $derived(fullTracks(view?.default));
	const shownTracks = $derived(custom ? draft : defaultTracks);
	const ocrOffered = $derived(
		view?.subtitle_ocr != null && (shownTracks.subtitle_output === 'separate' || shownTracks.subtitle_output === 'both')
	);
	const ocrOn = $derived(ocrOverride ?? view?.subtitle_ocr?.default ?? false);

	function adopt(v: JobTrackSelectionView): void {
		view = v;
		custom = v.override != null;
		draft = fullTracks(v.override ?? v.default);
		dirty = false;
		ocrOverride = v.subtitle_ocr?.override ?? null;
		ocrDirty = false;
	}

	async function load(): Promise<void> {
		loading = true;
		loadError = null;
		try {
			adopt(await fetchTrackSelection(jobId, sessionId));
		} catch (e) {
			loadError = e instanceof Error ? e.message : 'Could not load the track choice';
		} finally {
			loading = false;
		}
	}

	onMount(load);

	// Apply session: the default follows the chosen session.
	let lastSession = sessionId;
	$effect(() => {
		if (variant === 'apply' && sessionId !== lastSession) {
			lastSession = sessionId;
			void load();
		}
	});

	function setCustom(on: boolean): void {
		custom = on;
		if (on && !view?.override) draft = { ...defaultTracks };
		dirty = true;
		saved = false;
	}

	function edit(v: TrackSettings): void {
		draft = v;
		dirty = true;
		saved = false;
	}

	function setOcr(on: boolean): void {
		ocrOverride = on;
		ocrDirty = true;
		saved = false;
	}

	/** What the job should carry: its own choice, or null for the default. */
	export function value(): TrackSettings | null {
		return custom ? { ...draft } : null;
	}

	/** The text subtitles choice to send: only when it is offered and was changed. */
	export function ocrChoice(): { subtitle_ocr?: boolean | null } {
		return ocrOffered && ocrDirty ? { subtitle_ocr: ocrOverride } : {};
	}

	/** True once the job's track choice has loaded. */
	export function loaded(): boolean {
		return view !== null;
	}

	/** False while a language choice has no language. */
	export function valid(): boolean {
		return !custom || tracksValid(draft);
	}

	/** Save a changed choice (review Start). Throws when it cannot be saved. */
	export async function save(): Promise<void> {
		if (!view || locked || !(dirty || ocrDirty)) return;
		if (!valid()) throw new Error('Add at least one language, or choose all tracks.');
		saving = true;
		saveError = null;
		try {
			adopt(await saveTrackSelection(jobId, value(), ocrChoice()));
			saved = true;
		} catch (e) {
			saveError = e instanceof Error ? e.message : 'Saving the track choice failed';
			throw e;
		} finally {
			saving = false;
		}
	}

	async function saveClicked(): Promise<void> {
		try {
			await save();
		} catch {
			// shown through saveError (or the validity note)
		}
	}
</script>

<div class="stack job-track-picker" data-testid="job-track-picker">
	{#if loading && !view}
		<p class="field-help" data-testid="jtp-loading">Loading the disc's tracks...</p>
	{:else if loadError}
		<p class="field-error" data-testid="jtp-load-error">{loadError}</p>
	{:else if view}
		<p class="field-help" data-testid="jtp-default">
			Default ({view.default_source}): {describeTracks(defaultTracks)}
		</p>
		{#if !view.disc.known}
			<p class="field-help" data-testid="jtp-disc-unknown">
				The disc scan has no track list, so the disc's languages are not shown.
			</p>
		{/if}
		{#if locked}
			<p class="field-help" data-testid="jtp-locked">{view.locked_reason}</p>
		{/if}
		<label class="field-row">
			<input
				type="checkbox"
				data-testid="jtp-custom"
				checked={custom}
				disabled={!editable}
				onchange={(e) => setCustom(e.currentTarget.checked)}
			/>
			<span class="field-label">Choose tracks for this disc</span>
		</label>
		{#if custom}
			<TrackSelectionEditor value={draft} onchange={edit} prefix="jtp" disabled={!editable} disc={view.disc} />
		{:else if view.disc.known}
			<TrackSelectionEditor value={defaultTracks} onchange={() => {}} prefix="jtp-default" disabled disc={view.disc} />
		{/if}
		{#if ocrOffered}
			<label class="field-row">
				<input
					type="checkbox"
					data-testid="jtp-ocr"
					checked={ocrOn}
					disabled={!editable}
					onchange={(e) => setOcr(e.currentTarget.checked)}
				/>
				<span class="field-label">Make text subtitles (.srt)</span>
			</label>
			<p class="field-help" data-testid="jtp-ocr-help">
				Read from the Blu-ray subtitle images after each encode. Default: {view.subtitle_ocr?.default ? 'on' : 'off'}.
			</p>
		{/if}
		{#if saveError}
			<p class="field-error" data-testid="jtp-save-error">{saveError}</p>
		{/if}
		{#if variant === 'page' && editable}
			<div class="job-track-picker-actions">
				{#if saved && !dirty && !ocrDirty}
					<span class="field-help" data-testid="jtp-saved">Saved.</span>
				{/if}
				<button
					type="button"
					class="btn btn-primary btn-sm"
					data-testid="jtp-save"
					disabled={!(dirty || ocrDirty) || saving || !valid()}
					onclick={saveClicked}
				>
					{saving ? 'Saving...' : 'Save tracks'}
				</button>
			</div>
		{/if}
	{/if}
</div>

<style>
	.job-track-picker-actions {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.75rem;
	}
</style>
