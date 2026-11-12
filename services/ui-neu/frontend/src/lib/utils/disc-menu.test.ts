import { describe, expect, it } from 'vitest';
import type { MenuPreviewButtonView, MenuPreviewView, MenuProposalView } from '$lib/types/api.gen';
import {
	acceptBlocker,
	boxPercent,
	buttonHint,
	canRip,
	draftOf,
	isOpen,
	isPreviewLive,
	previewState,
	roleLabel,
	statusText
} from './disc-menu';

const proposal = (over: Partial<MenuProposalView> = {}): MenuProposalView => ({
	id: 'mnp_1',
	job_id: 'job_1',
	track_id: 'trk_8',
	page: '2',
	button: 12,
	label: 'Promo Videos',
	score: 0.99,
	plays: '00023.mpls',
	role: 'trailer',
	name: 'Promo Videos',
	episode_number: null,
	note: null,
	status: 'proposed',
	applied_path: null,
	last_error: null,
	created_at: '2026-11-12T00:00:00Z',
	updated_at: '2026-11-12T00:00:00Z',
	...over
});

const preview = (over: Partial<MenuPreviewView> = {}): MenuPreviewView => ({
	task_id: 'txt_1',
	status: 'done',
	progress_pct: 100,
	...over
});

describe('disc menu helpers', () => {
	it('names roles', () => {
		expect(roleLabel('trailer')).toBe('Trailer');
		expect(roleLabel('special')).toBe('Special (season 0)');
		expect(roleLabel('episode')).toBe('Not set');
		expect(roleLabel(null)).toBe('Not set');
	});

	it('says why a proposal cannot be accepted yet', () => {
		const p = proposal();
		expect(acceptBlocker(p, draftOf(p))).toBeNull();
		expect(acceptBlocker(proposal({ track_id: null }), draftOf(p))).toMatch(/not ripped/);
		expect(acceptBlocker(p, { ...draftOf(p), role: 'episode' })).toBe('Pick a role');
		expect(acceptBlocker(p, { ...draftOf(p), name: '  ' })).toBe('Give it a name');
		expect(acceptBlocker(p, { ...draftOf(p), role: 'special' })).toMatch(/episode number/);
		expect(acceptBlocker(p, { ...draftOf(p), role: 'special', episode: 3 })).toBeNull();
		expect(draftOf(proposal({ name: null }))).toEqual({ role: 'trailer', name: '', episode: null });
	});

	it('knows which proposals are still open', () => {
		expect(isOpen(proposal())).toBe(true);
		expect(isOpen(proposal({ status: 'rejected' }))).toBe(true);
		expect(isOpen(proposal({ status: 'applied' }))).toBe(false);
		expect(isOpen(proposal({ status: 'info' }))).toBe(false);
	});

	it('waits for the rip before the encode', () => {
		const accepted = proposal({ status: 'accepted' });
		expect(statusText(accepted, 'queued')).toBe('Waiting for the rip');
		expect(statusText(accepted, 'in_progress')).toBe('Waiting for the rip');
		expect(statusText(accepted, 'failed')).toBe('The rip failed');
		expect(statusText(accepted, 'done')).toBe('Waiting for the encode');
		expect(statusText(accepted, undefined)).toBe('Waiting for the encode');
		expect(statusText(proposal(), 'queued')).toBe('Proposed');
	});

	it('knows which menu titles it can rip', () => {
		const op = proposal({ track_id: null, scan_title: 19, role: 'other', name: 'Textless OP', status: 'info' });
		expect(canRip(op)).toBe(true);
		expect(canRip({ ...op, track_id: 'trk_19' })).toBe(false);
		expect(canRip({ ...op, scan_title: null })).toBe(false);
		expect(canRip({ ...op, scan_title: undefined })).toBe(false);
		expect(canRip({ ...op, role: null })).toBe(false);
		expect(canRip({ ...op, role: 'episode' })).toBe(false);
		expect(canRip({ ...op, name: ' ' })).toBe(false);
		expect(canRip({ ...op, role: 'special' })).toBe(false);
		expect(canRip({ ...op, role: 'special', episode_number: 4 })).toBe(true);
	});

	it('places a box as percentages of its page', () => {
		expect(boxPercent([480, 270, 960, 540], [1920, 1080])).toEqual({
			x: '25.000%',
			y: '25.000%',
			w: '25.000%',
			h: '25.000%'
		});
		expect(boxPercent([0, 0, 0, 0], [1920, 1080])).toBeNull();
		expect(boxPercent([0, 0, 5, 5], [0, 1080])).toBeNull();
	});

	it('describes what a button does', () => {
		const b = (over: Partial<MenuPreviewButtonView>): MenuPreviewButtonView => ({
			id: 1,
			box: [0, 0, 1, 1],
			visible: true,
			outcome: 'plays',
			...over
		});
		const name = (id: string) => `page ${id}`;
		expect(buttonHint(b({ label: 'Play All', plays: '00008.mpls' }), name)).toBe('"Play All": plays 00008.mpls');
		expect(buttonHint(b({}), name)).toBe('No label: plays a title');
		expect(buttonHint(b({ outcome: 'page', goes_to: '2' }), name)).toBe('No label: opens page 2');
		expect(buttonHint(b({ outcome: 'page' }), name)).toBe('No label: opens a page the walk did not reach');
		expect(buttonHint(b({ outcome: 'stays' }), name)).toBe('No label: stays on this page');
		expect(buttonHint(b({ outcome: 'stopped' }), name)).toBe('No label: does nothing the preview can follow');
	});

	it('sums up the preview', () => {
		expect(previewState(null)).toBe('The menu has not been read yet.');
		expect(previewState(preview({ status: 'queued' }))).toBe('Waiting to read the menu.');
		expect(previewState(preview({ status: 'in_progress', progress_pct: 40 }))).toBe('Reading the menu (40%).');
		expect(previewState(preview({ status: 'failed', last_error: 'no walk' }))).toBe('Reading the menu failed: no walk');
		expect(previewState(preview({ status: 'failed' }))).toBe('Reading the menu failed: no reason given');
		expect(previewState(preview({ pages: [] }))).toBe('0 pages read from the menu.');
		expect(
			previewState(
				preview({ pages: [{ id: '1', at: 'x', language: 'eng', image: 'pages/1.jpg', size: [1, 1], buttons: [] }] })
			)
		).toBe('1 page read from the menu.');
		expect(isPreviewLive(preview({ status: 'queued' }))).toBe(true);
		expect(isPreviewLive(preview())).toBe(false);
		expect(isPreviewLive(null)).toBe(false);
	});
});
