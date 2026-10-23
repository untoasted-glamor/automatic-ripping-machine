import type { TrackView } from '$lib/types/api.gen';
import { formatDuration } from './duration';
import { formatBytes } from './format';

/** "Keeping N of M titles, ~X GB, H:MM:SS": the kept tracks' size and length,
 * from the rip once it has them, else from the disc scan. Parts with nothing
 * known are left out. */
export function keepSummary(tracks: TrackView[], noun = 'titles'): string {
	const kept = tracks.filter((t) => !t.excluded);
	const bytes = kept.reduce((n, t) => n + (t.size_bytes ?? t.expected_size_bytes ?? 0), 0);
	const seconds = kept.reduce((n, t) => n + (t.duration_seconds ?? t.expected_duration_seconds ?? 0), 0);
	const parts = [`Keeping ${kept.length} of ${tracks.length} ${noun}`];
	if (bytes > 0) parts.push(`~${formatBytes(bytes)}`);
	if (seconds > 0) parts.push(formatDuration(seconds));
	return parts.join(', ');
}

/** Why a track's Keep box can't be changed for a job in `status`, or null. */
export function keepLockedReason(status: string): string | null {
	return status === 'ripping' ? "Titles can't be changed while the disc is ripping" : null;
}

/** A track never ripped because it was left out before the rip shows as skipped. */
export function trackRipStatus(track: TrackView, ripStarted: boolean): string {
	return ripStarted && track.excluded && track.status === 'queued' ? 'skipped' : track.status;
}

/** POST /retry-failed armed the job and its retry hasn't started yet. */
export function isRetryArmed(metadata: Record<string, unknown> | null | undefined): boolean {
	const flags = metadata?.flags;
	return typeof flags === 'object' && flags !== null && (flags as Record<string, unknown>).retry_armed === true;
}
