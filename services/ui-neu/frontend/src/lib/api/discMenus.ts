// The job page's Disc menu panel (fork only, disc menus M3-M4, A4): the menu's
// pages and buttons from the preview, the names the menu proposes for the
// job's titles, and the menu pack Jellyfin plays. Accepting a name renames the
// finished encode into the library's trailers/, other/, extras/ or Season 00
// folder.
import type {
	JobDiscMenuView,
	MenuProposalAcceptAllResponse,
	MenuProposalCreate,
	MenuProposalUpdate,
	MenuProposalView
} from '$lib/types/api.gen';
import { del, fetchObjectUrl, get, patch, post } from './client';

const base = (jobId: string) => `/api/jobs/${jobId}/disc-menu`;

export function fetchDiscMenu(jobId: string): Promise<JobDiscMenuView> {
	return get<JobDiscMenuView>(base(jobId));
}

/** An object URL for one of the preview's images (`pages/1.jpg`, `crops/2.b12.png`). */
export function fetchDiscMenuImage(jobId: string, relative: string): Promise<string> {
	return fetchObjectUrl(`${base(jobId)}/files/${relative}`);
}

export function requestDiscMenuPreview(jobId: string): Promise<JobDiscMenuView> {
	return post<JobDiscMenuView>(`${base(jobId)}/preview`);
}

export function updateMenuProposal(
	jobId: string,
	proposalId: string,
	body: MenuProposalUpdate
): Promise<MenuProposalView> {
	return patch<MenuProposalView>(`${base(jobId)}/proposals/${proposalId}`, body);
}

export function createMenuProposal(jobId: string, body: MenuProposalCreate): Promise<MenuProposalView> {
	return post<MenuProposalView>(`${base(jobId)}/proposals`, body);
}

/** Rip the title a proposal's button plays (one the job has no track for) from the kept disc backup. */
export function ripMenuTitle(jobId: string, proposalId: string): Promise<MenuProposalView> {
	return post<MenuProposalView>(`${base(jobId)}/proposals/${proposalId}/rip`);
}

export function acceptAllMenuProposals(jobId: string): Promise<MenuProposalAcceptAllResponse> {
	return post<MenuProposalAcceptAllResponse>(`${base(jobId)}/proposals/accept-all`);
}

/** Build the menu pack again (from the copied menu and the job's encodes). */
export function buildDiscMenuPack(jobId: string): Promise<JobDiscMenuView> {
	return post<JobDiscMenuView>(`${base(jobId)}/pack`);
}

/** Remove the menu pack from the show folder; Build makes it again. */
export function deleteDiscMenuPack(jobId: string): Promise<JobDiscMenuView> {
	return del<JobDiscMenuView>(`${base(jobId)}/pack`);
}
