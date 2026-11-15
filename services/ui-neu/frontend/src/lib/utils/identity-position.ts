import type { JobView } from '$lib/types/api.gen';

/** The disc's place in a set, as the operator types it. Nothing on a disc records a
 *  season, and a box set's disc number often isn't in the label, so these fill the
 *  `{season}` / `{disc}` naming tokens. */
export interface DiscPosition {
	season: number | null;
	discNumber: number | null;
	discTotal: number | null;
}

export type PositionChange = { season?: number | null; disc_number?: number | null; disc_total?: number | null };

export function numOrNull(v: number | string | null | undefined): number | null {
	if (v == null || v === '') return null;
	const n = Number(v);
	return Number.isFinite(n) ? n : null;
}

export function positionOf(job: Pick<JobView, 'season' | 'disc_number' | 'disc_total'>): DiscPosition {
	return { season: job.season ?? null, discNumber: job.disc_number ?? null, discTotal: job.disc_total ?? null };
}

/** Only the fields that changed: resolve keeps an omitted field as it is, and reads null as "clear it".
 *  A CD has no season. */
export function changedPosition(
	job: Pick<JobView, 'season' | 'disc_number' | 'disc_total'>,
	p: DiscPosition,
	{ isCd = false }: { isCd?: boolean } = {}
): PositionChange {
	const out: PositionChange = {};
	if (!isCd && numOrNull(p.season) !== (job.season ?? null)) out.season = numOrNull(p.season);
	if (numOrNull(p.discNumber) !== (job.disc_number ?? null)) out.disc_number = numOrNull(p.discNumber);
	if (numOrNull(p.discTotal) !== (job.disc_total ?? null)) out.disc_total = numOrNull(p.discTotal);
	return out;
}

// The job waits for the operator to identify it, or at stop 1 (identified before the backup). By stop 2
// (`awaiting_titles`) the identity is settled: that stop is for the titles.
const OPENS_IDENTITY = new Set(['awaiting_user_id', 'ripped_awaiting_identify', 'awaiting_review']);

/** Whether the job page's Identity panel opens by itself for a job in `status`. */
export function identityOpensFor(status: string): boolean {
	return OPENS_IDENTITY.has(status);
}
