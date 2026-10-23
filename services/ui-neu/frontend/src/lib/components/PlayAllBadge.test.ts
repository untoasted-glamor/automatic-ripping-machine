import { describe, it, expect, afterEach } from 'vitest';
import { renderComponent, screen, cleanup } from '$lib/test-utils';
import PlayAllBadge from './PlayAllBadge.svelte';
import { createTrack } from './__fixtures__/job';

describe('PlayAllBadge', () => {
	afterEach(() => cleanup());

	it("says the detector's call is a likely play-all, and why it starts left out", () => {
		renderComponent(PlayAllBadge, {
			props: { track: createTrack({ role: 'play_all', identity_provenance: { role: 'play_all_detector' } }) }
		});
		const badge = screen.getByTestId('play-all-badge');
		expect(badge).toHaveTextContent('likely play-all');
		expect(badge.getAttribute('title')).toContain('It starts left out');
	});

	it('names a play-all another source set plainly, and shows nothing for other roles', () => {
		renderComponent(PlayAllBadge, {
			props: { track: createTrack({ role: 'play_all', identity_provenance: { role: 'manual' } }) }
		});
		expect(screen.getByTestId('play-all-badge')).toHaveTextContent(/^\s*play-all\s*$/);
		cleanup();
		renderComponent(PlayAllBadge, { props: { track: createTrack({ role: 'extra' }) } });
		expect(screen.queryByTestId('play-all-badge')).toBeNull();
	});
});
