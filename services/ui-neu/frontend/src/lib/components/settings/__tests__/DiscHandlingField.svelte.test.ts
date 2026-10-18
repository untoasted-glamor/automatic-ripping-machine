import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup, waitFor } from '$lib/test-utils';
import DiscHandlingField from '../DiscHandlingField.svelte';

// `.svelte.test.ts` so `$state` works: the field writes into the parent
// form's reactive values object, as SchemaConfigForm's is.

const fetchDrives = vi.fn(() => Promise.resolve([] as unknown[]));
vi.mock('$lib/api/drives', () => ({ fetchDrives: () => fetchDrives() }));

// The live pause state comes from the dashboard poll once it has answered.
const dash = vi.hoisted(() => ({ ready: false, rippingEnabled: true }));
vi.mock('$lib/stores/dashboard', async () => {
	const { readable } = await import('svelte/store');
	return {
		dashboard: {
			subscribe: (fn: (v: unknown) => void) =>
				readable({ db_available: true, ripping_enabled: dash.rippingEnabled }).subscribe(fn),
			initialized: { subscribe: (fn: (v: boolean) => void) => readable(dash.ready).subscribe(fn) }
		}
	};
});

const PAUSED = 'Paused: All new rips paused until you resume';

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	dash.ready = false;
	dash.rippingEnabled = true;
});

describe('DiscHandlingField', () => {
	it('maps the three options onto auto_rip_on_insert + hold_for_review', async () => {
		const values: Record<string, unknown> = $state({ auto_rip_on_insert: true, hold_for_review: false });
		renderComponent(DiscHandlingField, { props: { values, context: { manual_wait_seconds: 45 } } });
		expect(screen.getByRole('radio', { name: /fully automatic/i })).toBeChecked();
		expect(screen.getByText(/waits 45 seconds/i)).toBeInTheDocument();
		expect(screen.getByText('Suggested')).toBeInTheDocument();

		await fireEvent.click(screen.getByRole('radio', { name: /review first/i }));
		expect(values).toMatchObject({ auto_rip_on_insert: true, hold_for_review: true });

		await fireEvent.click(screen.getByRole('radio', { name: /manual/i }));
		expect(values.auto_rip_on_insert).toBe(false);
		expect(values.hold_for_review).toBe(true); // Manual leaves the review setting alone
	});

	it('describes Review first as waiting for a confirm in mandatory-confirm mode', () => {
		renderComponent(DiscHandlingField, {
			props: {
				values: { auto_rip_on_insert: true, hold_for_review: true },
				context: { manual_wait_seconds: 45, review_requires_confirm: true }
			}
		});
		expect(screen.getByText(/nothing rips until you do/i)).toBeInTheDocument();
		expect(screen.queryByText(/waits 45 seconds/i)).not.toBeInTheDocument();
	});

	it('reads the current choice', () => {
		renderComponent(DiscHandlingField, { props: { values: { auto_rip_on_insert: false, hold_for_review: false } } });
		expect(screen.getByRole('radio', { name: /manual/i })).toBeChecked();
	});

	it('notes drives with their own mode', async () => {
		fetchDrives.mockResolvedValueOnce([
			{ id: 'a', lifecycle: 'enrolled', drive_mode: 'manual' },
			{ id: 'b', lifecycle: 'enrolled', drive_mode: null },
			{ id: 'c', lifecycle: 'detected', drive_mode: 'auto' }
		]);
		renderComponent(DiscHandlingField, { props: { values: { auto_rip_on_insert: true } } });
		await waitFor(() =>
			expect(screen.getByTestId('disc-handling-overrides')).toHaveTextContent(
				"1 drive has its own setting and won't change."
			)
		);
	});

	describe('Review first sub-choice', () => {
		it('shows only while Review first is selected', async () => {
			const values: Record<string, unknown> = $state({
				auto_rip_on_insert: true,
				hold_for_review: false,
				review_requires_confirm: false,
				manual_wait_seconds: 60
			});
			renderComponent(DiscHandlingField, { props: { values } });
			expect(screen.queryByTestId('disc-handling-review')).not.toBeInTheDocument();

			await fireEvent.click(screen.getByRole('radio', { name: /review first/i }));
			expect(screen.getByTestId('disc-handling-review')).toBeInTheDocument();
			expect(screen.getByRole('radio', { name: /start after a countdown/i })).toBeChecked();

			await fireEvent.click(screen.getByRole('radio', { name: /manual/i }));
			expect(screen.queryByTestId('disc-handling-review')).not.toBeInTheDocument();
		});

		it('edits the countdown and flags an empty box', async () => {
			const values: Record<string, unknown> = $state({
				auto_rip_on_insert: true,
				hold_for_review: true,
				review_requires_confirm: false,
				manual_wait_seconds: 60
			});
			renderComponent(DiscHandlingField, { props: { values } });
			const box = screen.getByTestId('manual-wait-seconds') as HTMLInputElement;
			expect(box.value).toBe('60');

			await fireEvent.input(box, { target: { value: '120' } });
			expect(values.manual_wait_seconds).toBe(120);
			expect(screen.getByText(/waits 120 seconds/i)).toBeInTheDocument();

			await fireEvent.input(box, { target: { value: '' } });
			expect(values.manual_wait_seconds).toBeNull();
			expect(screen.getByTestId('manual-wait-seconds-error')).toHaveTextContent(/Enter a countdown/);
		});

		it('Hold until I confirm sets review_requires_confirm and hides the countdown', async () => {
			const values: Record<string, unknown> = $state({
				auto_rip_on_insert: true,
				hold_for_review: true,
				review_requires_confirm: false,
				manual_wait_seconds: 45
			});
			renderComponent(DiscHandlingField, { props: { values } });
			await fireEvent.click(screen.getByRole('radio', { name: /hold until i confirm/i }));
			expect(values.review_requires_confirm).toBe(true);
			expect(values.manual_wait_seconds).toBe(45); // a valid countdown is kept for later
			expect(screen.queryByTestId('manual-wait-seconds')).not.toBeInTheDocument();
			expect(screen.getByText(/nothing rips until you do/i)).toBeInTheDocument();

			await fireEvent.click(screen.getByRole('radio', { name: /start after a countdown/i }));
			expect(values.review_requires_confirm).toBe(false);
			expect((screen.getByTestId('manual-wait-seconds') as HTMLInputElement).value).toBe('45');
		});

		it('repairs a half-typed countdown when it is hidden', async () => {
			const values: Record<string, unknown> = $state({
				auto_rip_on_insert: true,
				hold_for_review: true,
				review_requires_confirm: false,
				manual_wait_seconds: null
			});
			renderComponent(DiscHandlingField, { props: { values, context: { manual_wait_seconds: 90 } } });
			await fireEvent.click(screen.getByRole('radio', { name: /hold until i confirm/i }));
			expect(values.manual_wait_seconds).toBe(90); // the saved value, not null

			values.review_requires_confirm = false;
			values.manual_wait_seconds = -1;
			await fireEvent.click(screen.getByRole('radio', { name: /fully automatic/i }));
			expect(values.manual_wait_seconds).toBe(90);
		});
	});

	describe('while ripping is paused', () => {
		it('locks the choice and says so (saved config, before the dashboard answers)', () => {
			renderComponent(DiscHandlingField, {
				props: {
					values: { auto_rip_on_insert: true, hold_for_review: true, review_requires_confirm: false },
					context: { ripping_paused: true, manual_wait_seconds: 60 }
				}
			});
			expect(screen.getByTestId('disc-handling-paused')).toHaveTextContent(PAUSED);
			for (const radio of screen.getAllByRole('radio')) expect(radio).toBeDisabled();
			expect(screen.getByTestId('manual-wait-seconds')).toBeDisabled();
		});

		it('follows the live dashboard once it has answered', () => {
			dash.ready = true;
			dash.rippingEnabled = false;
			renderComponent(DiscHandlingField, {
				props: { values: { auto_rip_on_insert: true, hold_for_review: true }, context: { ripping_paused: false } }
			});
			expect(screen.getByTestId('disc-handling-paused')).toHaveTextContent(PAUSED);
			expect(screen.getByRole('radio', { name: /fully automatic/i })).toBeDisabled();
		});

		it('is unlocked once resumed, even if the saved config still says paused', () => {
			dash.ready = true;
			dash.rippingEnabled = true;
			renderComponent(DiscHandlingField, {
				props: { values: { auto_rip_on_insert: true, hold_for_review: true }, context: { ripping_paused: true } }
			});
			expect(screen.queryByTestId('disc-handling-paused')).not.toBeInTheDocument();
			expect(screen.getByRole('radio', { name: /fully automatic/i })).toBeEnabled();
		});
	});
});
