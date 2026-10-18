import { describe, it, expect, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup } from '$lib/test-utils';
import ReviewWaitField from '../ReviewWaitField.svelte';

// `.svelte.test.ts` so `$state` works: the field writes into the parent
// form's reactive values object, as SchemaConfigForm's is.

afterEach(() => cleanup());

describe('ReviewWaitField', () => {
	it('shows the countdown box in timed mode and edits manual_wait_seconds', async () => {
		const values: Record<string, unknown> = $state({
			hold_for_review: true,
			review_requires_confirm: false,
			manual_wait_seconds: 60
		});
		renderComponent(ReviewWaitField, { props: { values } });
		expect(screen.getByRole('radio', { name: /start after a countdown/i })).toBeChecked();
		const box = screen.getByTestId('manual-wait-seconds') as HTMLInputElement;
		expect(box.value).toBe('60');

		await fireEvent.input(box, { target: { value: '120' } });
		expect(values.manual_wait_seconds).toBe(120);

		await fireEvent.input(box, { target: { value: '' } });
		expect(values.manual_wait_seconds).toBeNull();
		expect(screen.getByTestId('manual-wait-seconds-error')).toHaveTextContent(/Enter a countdown/);
	});

	it('choosing manual confirm sets review_requires_confirm and hides the countdown', async () => {
		const values: Record<string, unknown> = $state({
			hold_for_review: true,
			review_requires_confirm: false,
			manual_wait_seconds: 45
		});
		renderComponent(ReviewWaitField, { props: { values } });
		await fireEvent.click(screen.getByRole('radio', { name: /require manual confirm/i }));
		expect(values.review_requires_confirm).toBe(true);
		expect(values.manual_wait_seconds).toBe(45); // a valid countdown is kept for later
		expect(screen.queryByTestId('manual-wait-seconds')).not.toBeInTheDocument();

		await fireEvent.click(screen.getByRole('radio', { name: /start after a countdown/i }));
		expect(values.review_requires_confirm).toBe(false);
		expect((screen.getByTestId('manual-wait-seconds') as HTMLInputElement).value).toBe('45');
	});

	it('repairs a half-typed countdown when switching to manual confirm', async () => {
		const values: Record<string, unknown> = $state({
			hold_for_review: true,
			review_requires_confirm: false,
			manual_wait_seconds: null
		});
		renderComponent(ReviewWaitField, { props: { values, context: { manual_wait_seconds: 90 } } });
		await fireEvent.click(screen.getByRole('radio', { name: /require manual confirm/i }));
		expect(values.manual_wait_seconds).toBe(90); // the saved value, not null
	});

	it('reads mandatory mode and says when the gate is off', () => {
		renderComponent(ReviewWaitField, {
			props: { values: { hold_for_review: false, review_requires_confirm: true, manual_wait_seconds: 60 } }
		});
		expect(screen.getByRole('radio', { name: /require manual confirm/i })).toBeChecked();
		expect(screen.getByText(/set to Review first/i)).toBeInTheDocument();
	});
});
