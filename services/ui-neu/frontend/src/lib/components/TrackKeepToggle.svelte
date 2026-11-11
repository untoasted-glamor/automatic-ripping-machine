<script lang="ts">
	import { updateTrack } from '$lib/api/jobs';
	import type { TrackView } from '$lib/types/api.gen';
	import { isDiscMenu, menuSkippedReason } from '$lib/utils/track-keep';

	// The Keep checkbox for one track, shared by the job page and the dashboard
	// review card. Unticking before the rip means the title is never ripped;
	// after it, that the title is left out of transcodes.
	interface Props {
		jobId: string;
		track: TrackView;
		/** Why the box can't be changed right now (keepLockedReason), or null. */
		lockedReason?: string | null;
		/** Before the rip, the only kept track can't be unticked. */
		lastKept?: boolean;
		/** The PATCH settled (either way): reload. */
		onchanged: () => void;
		/** The backend's reason a change was refused; null when a new one starts. */
		onerror: (message: string | null) => void;
	}

	let { jobId, track, lockedReason = null, lastKept = false, onchanged, onerror }: Props = $props();

	const KEEP_HELP =
		'Keep this title. One left out before the rip starts is not ripped (keep it afterwards and use Rip remaining titles to rip it then); one left out after it is left out of transcodes.';
	const MENU_HELP =
		"Copy this disc's menus with its titles, so the library can show them. Ticked after the rip, Rip remaining titles copies them then.";
	let menu = $derived(isDiscMenu(track));
	// A skipped disc menu can't be ripped at all (Java menus, no menu): its reason locks the box.
	let locked = $derived(menuSkippedReason(track) ?? lockedReason);
	let lastOne = $derived(lastKept && !track.excluded && !menu);
	let title = $derived(locked ?? (lastOne ? 'At least one title has to be kept' : menu ? MENU_HELP : KEEP_HELP));

	async function toggle(input: HTMLInputElement) {
		const excluded = !input.checked;
		onerror(null);
		try {
			await updateTrack(jobId, track.id, { excluded });
		} catch (e) {
			// e.g. a 409 for keeping a track that has no ripped file. The server
			// value is unchanged, so the reload re-renders nothing: undo the click
			// here. Checked means kept, and the refused request was `excluded`.
			onerror(`Could not update the track: ${e instanceof Error ? e.message : 'unknown error'}`);
			input.checked = excluded;
		} finally {
			onchanged();
		}
	}
</script>

<input
	type="checkbox"
	class="track-keep-toggle"
	checked={!track.excluded}
	disabled={locked != null || lastOne}
	{title}
	aria-label={menu ? 'Keep the disc menu' : `Keep track ${track.index}`}
	onchange={(e) => toggle(e.currentTarget as HTMLInputElement)}
/>

<style>
	.track-keep-toggle {
		width: 1rem;
		height: 1rem;
		border-radius: var(--radius-sm);
		accent-color: var(--color-primary);
		cursor: pointer;
	}
	.track-keep-toggle:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}
</style>
