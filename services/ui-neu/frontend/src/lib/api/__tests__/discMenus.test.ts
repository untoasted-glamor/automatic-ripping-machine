import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
	acceptAllMenuProposals,
	buildDiscMenuPack,
	createMenuProposal,
	deleteDiscMenuPack,
	fetchDiscMenu,
	fetchDiscMenuImage,
	requestDiscMenuPreview,
	ripMenuTitle,
	updateMenuProposal
} from '../discMenus';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

function jsonResponse(data: unknown, ok = true, status = 200, statusText = 'OK') {
	return { ok, status, statusText, json: () => Promise.resolve(data), blob: () => Promise.resolve(new Blob(['x'])) };
}

beforeEach(() => {
	mockFetch.mockReset();
	localStorage.clear();
});

describe('disc menu API', () => {
	it('reads the panel, and asks for another preview', async () => {
		mockFetch.mockResolvedValue(jsonResponse({ track_id: 'trk_menu' }));
		expect(await fetchDiscMenu('job_1')).toEqual({ track_id: 'trk_menu' });
		expect(mockFetch.mock.calls[0][0]).toBe('/api/jobs/job_1/disc-menu');
		await requestDiscMenuPreview('job_1');
		expect(mockFetch.mock.calls[1][0]).toBe('/api/jobs/job_1/disc-menu/preview');
		expect(mockFetch.mock.calls[1][1].method).toBe('POST');
	});

	it('builds and deletes the menu pack', async () => {
		mockFetch.mockResolvedValue(jsonResponse({ track_id: 'trk_menu' }));
		await buildDiscMenuPack('job_1');
		expect(mockFetch.mock.calls[0][0]).toBe('/api/jobs/job_1/disc-menu/pack');
		expect(mockFetch.mock.calls[0][1].method).toBe('POST');
		await deleteDiscMenuPack('job_1');
		expect(mockFetch.mock.calls[1][0]).toBe('/api/jobs/job_1/disc-menu/pack');
		expect(mockFetch.mock.calls[1][1].method).toBe('DELETE');
	});

	it('edits, adds and accepts names', async () => {
		mockFetch.mockResolvedValue(jsonResponse({}));
		await updateMenuProposal('job_1', 'mnp_1', { action: 'accept', name: 'PV' });
		expect(mockFetch.mock.calls[0][0]).toBe('/api/jobs/job_1/disc-menu/proposals/mnp_1');
		expect(mockFetch.mock.calls[0][1].method).toBe('PATCH');
		expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ action: 'accept', name: 'PV' });
		await createMenuProposal('job_1', { track_id: 'trk_7', role: 'extra', name: 'Panel' });
		expect(mockFetch.mock.calls[1][0]).toBe('/api/jobs/job_1/disc-menu/proposals');
		expect(JSON.parse(mockFetch.mock.calls[1][1].body)).toEqual({ track_id: 'trk_7', role: 'extra', name: 'Panel' });
		await acceptAllMenuProposals('job_1');
		expect(mockFetch.mock.calls[2][0]).toBe('/api/jobs/job_1/disc-menu/proposals/accept-all');
		await ripMenuTitle('job_1', 'mnp_op');
		expect(mockFetch.mock.calls[3][0]).toBe('/api/jobs/job_1/disc-menu/proposals/mnp_op/rip');
		expect(mockFetch.mock.calls[3][1].method).toBe('POST');
	});

	it('fetches a preview image with the token, as an object URL', async () => {
		localStorage.setItem('arm_token', 'tok');
		const create = vi.fn(() => 'blob:page');
		vi.stubGlobal('URL', { ...URL, createObjectURL: create });
		mockFetch.mockResolvedValue(jsonResponse(null));
		expect(await fetchDiscMenuImage('job_1', 'pages/1.jpg')).toBe('blob:page');
		expect(mockFetch.mock.calls[0][0]).toBe('/api/jobs/job_1/disc-menu/files/pages/1.jpg');
		expect(mockFetch.mock.calls[0][1].headers).toEqual({ Authorization: 'Bearer tok' });
		mockFetch.mockResolvedValue(jsonResponse({ detail: 'no such file' }, false, 404, 'Not Found'));
		await expect(fetchDiscMenuImage('job_1', 'pages/9.jpg')).rejects.toThrow('no such file');
		vi.unstubAllGlobals();
		vi.stubGlobal('fetch', mockFetch);
	});
});
