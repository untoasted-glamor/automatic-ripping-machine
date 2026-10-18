// `H:MM:SS`, or `M:SS` under an hour (1452 → "24:12", 13078 → "3:37:58").
// Feature-length titles routinely run past an hour, so plain `M:SS` would read
// "217:58" for a 3h37m title.
export function formatDuration(seconds: number): string {
	const total = Math.max(0, Math.round(seconds));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = total % 60;
	const ss = String(s).padStart(2, '0');
	if (h === 0) return `${m}:${ss}`;
	return `${h}:${String(m).padStart(2, '0')}:${ss}`;
}

// A track's length for the job page, mirroring the Size column: the post-rip
// actual wins; otherwise MakeMKV's scan-time estimate, flagged `estimated` so
// the caller can render it as `~` + dimmed; `null` when neither is known.
export function trackDuration(
	actual: number | null | undefined,
	expected: number | null | undefined
): { label: string; estimated: boolean } | null {
	if (actual != null) return { label: formatDuration(actual), estimated: false };
	if (expected != null) return { label: formatDuration(expected), estimated: true };
	return null;
}
