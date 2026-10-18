import type { ScanTitle } from '$lib/types/api.gen';

/**
 * The titles of a stored disc scan (`metadata_json.scan_result`) worth showing:
 * those at or above the ripper's baseline minimum length
 * (`scan_result.min_length_seconds`), the same cut the backend makes for Track
 * rows. The ripper scans every title (MakeMKV `--minlength=0`), so without it a
 * Blu-ray lists dozens of menu clips. Scans without the field are shown as-is,
 * and a title of unknown duration is kept.
 */
export function listedScanTitles(scan: unknown): ScanTitle[] {
	if (!scan || typeof scan !== 'object') return [];
	const { titles, min_length_seconds: floor } = scan as {
		titles?: unknown;
		min_length_seconds?: unknown;
	};
	if (!Array.isArray(titles)) return [];
	const all = titles as ScanTitle[];
	if (typeof floor !== 'number') return all;
	return all.filter((t) => t.duration_seconds == null || t.duration_seconds >= floor);
}
