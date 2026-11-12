// The job page's Disc menu panel (fork only): labels and rules for the menu's
// name proposals, and where a menu button's box sits over its page image.
import type { MenuPreviewButtonView, MenuPreviewView, MenuProposalView, TrackRole } from '$lib/types/api.gen';

/** The roles a menu can name a title with: each moves the encode into its own library folder. */
export const MENU_ROLES: { value: TrackRole; label: string; folder: string }[] = [
	{ value: 'trailer', label: 'Trailer', folder: 'trailers' },
	{ value: 'other', label: 'Other (openings, endings)', folder: 'other' },
	{ value: 'extra', label: 'Extra', folder: 'extras' },
	{ value: 'special', label: 'Special (season 0)', folder: 'Season 00' }
];

export function roleLabel(role: TrackRole | null | undefined): string {
	return MENU_ROLES.find((r) => r.value === role)?.label ?? 'Not set';
}

export interface ProposalDraft {
	role: TrackRole | null;
	name: string;
	episode: number | null;
}

export function draftOf(p: MenuProposalView): ProposalDraft {
	return { role: p.role, name: p.name ?? '', episode: p.episode_number };
}

/** Why a draft can't be accepted yet, or null when it can. */
export function acceptBlocker(p: MenuProposalView, d: ProposalDraft): string | null {
	if (!p.track_id) return 'That button plays a title that was not ripped';
	if (!MENU_ROLES.some((r) => r.value === d.role)) return 'Pick a role';
	if (!d.name.trim()) return 'Give it a name';
	if (d.role === 'special' && !d.episode) return 'A special needs its season 0 episode number';
	return null;
}

export function isOpen(p: MenuProposalView): boolean {
	return p.status === 'proposed' || p.status === 'rejected';
}

export const PROPOSAL_STATUS: Record<MenuProposalView['status'], { text: string; tone: string }> = {
	proposed: { text: 'Proposed', tone: 'info' },
	accepted: { text: 'Waiting for the encode', tone: 'warning' },
	applied: { text: 'Renamed', tone: 'success' },
	rejected: { text: 'Rejected', tone: '' },
	info: { text: 'Note', tone: '' }
};

/** A button's box as percentages of its page (the image is scaled to fit). */
export function boxPercent(box: number[], size: number[]): { x: string; y: string; w: string; h: string } | null {
	const [x0, y0, x1, y1] = box;
	const [w, h] = size;
	if (!w || !h || x1 <= x0 || y1 <= y0) return null;
	const pct = (v: number, of: number) => `${((v / of) * 100).toFixed(3)}%`;
	return { x: pct(x0, w), y: pct(y0, h), w: pct(x1 - x0, w), h: pct(y1 - y0, h) };
}

/** What a button does, for its tooltip. */
export function buttonHint(b: MenuPreviewButtonView, pageName: (id: string) => string): string {
	const label = b.label ? `"${b.label}"` : 'No label';
	switch (b.outcome) {
		case 'plays':
			return `${label}: plays ${b.plays ?? 'a title'}`;
		case 'page':
			return `${label}: ${b.goes_to ? `opens ${pageName(b.goes_to)}` : 'opens a page the walk did not reach'}`;
		case 'stays':
			return `${label}: stays on this page`;
		default:
			return `${label}: does nothing the preview can follow`;
	}
}

/** One line on where the preview is. */
export function previewState(p: MenuPreviewView | null | undefined): string {
	if (!p) return 'The menu has not been read yet.';
	switch (p.status) {
		case 'queued':
			return 'Waiting to read the menu.';
		case 'in_progress':
			return `Reading the menu (${p.progress_pct}%).`;
		case 'failed':
			return `Reading the menu failed: ${p.last_error ?? 'no reason given'}`;
		default: {
			const n = p.pages?.length ?? 0;
			return `${n} ${n === 1 ? 'page' : 'pages'} read from the menu.`;
		}
	}
}

export function isPreviewLive(p: MenuPreviewView | null | undefined): boolean {
	return p?.status === 'queued' || p?.status === 'in_progress';
}
