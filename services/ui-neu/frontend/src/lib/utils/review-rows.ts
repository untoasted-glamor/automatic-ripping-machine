import type { TrackView, ScanTitle } from '$lib/types/api.gen';
import { isDiscMenu, menuSkippedReason } from './track-keep';

// One row shape covering both review-card lifecycle stages. A row is editable iff
// it has a real materialized-track id; scanned-title rows (trackId === null) are
// read-only.
export type ReviewRow = {
	index: number;
	durationSeconds: number | null;
	sourceLabel: string | null;
	trackId: string | null;
	title: string | null;
	year: number | null;
	episodeNumber: number | null;
	excluded: boolean;
	/** The "Disc menu" row: no number, identity, episode or length of its own. */
	isMenu: boolean;
	/** Why a disc menu is skipped, shown in its row. */
	note: string | null;
};

export function trackToRow(t: TrackView): ReviewRow {
	return {
		index: t.index,
		durationSeconds: t.duration_seconds,
		sourceLabel: t.output_path || t.source_ref,
		trackId: t.id,
		title: t.title ?? null,
		year: t.year ?? null,
		episodeNumber: t.episode_number ?? null,
		excluded: t.excluded ?? false,
		isMenu: isDiscMenu(t),
		note: menuSkippedReason(t)
	};
}

export function scanTitleToRow(s: ScanTitle): ReviewRow {
	return {
		index: s.index,
		durationSeconds: s.duration_seconds,
		sourceLabel: s.source_file ?? null,
		trackId: null,
		title: null,
		year: null,
		episodeNumber: null,
		excluded: false,
		isMenu: false,
		note: null
	};
}
