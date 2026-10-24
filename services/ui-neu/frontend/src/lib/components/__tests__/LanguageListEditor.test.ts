import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderComponent, screen, fireEvent, cleanup } from '$lib/test-utils';
import LanguageListEditor from '../LanguageListEditor.svelte';
import { canonicalLanguage, languageLabel } from '$lib/languages';

function render(value: string[], extra: Record<string, unknown> = {}) {
	const onchange = vi.fn();
	renderComponent(LanguageListEditor, {
		props: { value, onchange, label: 'Audio languages', testid: 'langs', ...extra }
	});
	return onchange;
}

describe('LanguageListEditor', () => {
	afterEach(() => cleanup());

	it('shows the empty state and offers every common language', () => {
		render([]);
		expect(screen.getByTestId('langs-empty')).toHaveTextContent('No languages added yet.');
		const pick = screen.getByTestId('langs-pick') as HTMLSelectElement;
		expect(pick.options[0].textContent).toBe('Add a language...');
		expect(Array.from(pick.options).some((o) => o.value === 'jpn')).toBe(true);
	});

	it('adds a picked language and leaves already listed ones out of the picker', async () => {
		const onchange = render(['eng']);
		const pick = screen.getByTestId('langs-pick') as HTMLSelectElement;
		expect(Array.from(pick.options).some((o) => o.value === 'eng')).toBe(false);
		await fireEvent.change(pick, { target: { value: 'jpn' } });
		expect(onchange).toHaveBeenCalledWith(['eng', 'jpn']);
		expect(pick.value).toBe('');
	});

	it('adds a typed code in its stored form, with Enter, and ignores a duplicate', async () => {
		const onchange = render(['ger']);
		const code = screen.getByTestId('langs-code') as HTMLInputElement;
		await fireEvent.input(code, { target: { value: 'FIN' } });
		await fireEvent.keyDown(code, { key: 'Enter' });
		expect(onchange).toHaveBeenLastCalledWith(['ger', 'fin']);
		expect(code.value).toBe('');

		onchange.mockClear();
		await fireEvent.input(code, { target: { value: 'deu' } });
		await fireEvent.click(screen.getByTestId('langs-add'));
		expect(onchange).not.toHaveBeenCalled();

		// Other keys do nothing; an empty field adds nothing.
		await fireEvent.keyDown(code, { key: 'a' });
		await fireEvent.input(code, { target: { value: '  ' } });
		await fireEvent.click(screen.getByTestId('langs-add'));
		expect(onchange).not.toHaveBeenCalled();
	});

	it('refuses a code that is not three letters', async () => {
		const onchange = render([]);
		const code = screen.getByTestId('langs-code') as HTMLInputElement;
		await fireEvent.input(code, { target: { value: 'en' } });
		await fireEvent.click(screen.getByTestId('langs-add'));
		expect(onchange).not.toHaveBeenCalled();
		expect(screen.getByTestId('langs-error')).toHaveTextContent('"en" is not an ISO 639-2 code');
		expect(code).toHaveAttribute('aria-invalid', 'true');
		expect(code.value).toBe('en');
	});

	it('reorders and removes entries; the first carries the note', async () => {
		const onchange = render(['eng', 'jpn', 'fre'], { firstNote: 'default track' });
		const items = screen.getAllByTestId('langs-item');
		expect(items[0]).toHaveTextContent('eng | default track');
		expect(items[1]).not.toHaveTextContent('default track');
		expect(screen.getByRole('button', { name: 'Move English up' })).toBeDisabled();
		expect(screen.getByRole('button', { name: 'Move French down' })).toBeDisabled();

		await fireEvent.click(screen.getByRole('button', { name: 'Move Japanese up' }));
		expect(onchange).toHaveBeenLastCalledWith(['jpn', 'eng', 'fre']);
		// The editor is controlled: each move starts from the `value` it was given.
		await fireEvent.click(screen.getByRole('button', { name: 'Move Japanese down' }));
		expect(onchange).toHaveBeenLastCalledWith(['eng', 'fre', 'jpn']);
		await fireEvent.click(screen.getByRole('button', { name: 'Remove Japanese' }));
		expect(onchange).toHaveBeenLastCalledWith(['eng', 'fre']);
	});

	it('shows an unknown code as itself and hides the add controls when disabled', () => {
		render(['tlh'], { disabled: true });
		expect(screen.getByTestId('langs-item')).toHaveTextContent('tlh');
		expect(screen.queryByTestId('langs-pick')).toBeNull();
		expect(screen.getByRole('button', { name: 'Remove tlh' })).toBeDisabled();
	});
});

describe('languages helpers', () => {
	it('canonicalises codes like the backend', () => {
		expect(canonicalLanguage(' DEU ')).toBe('ger');
		expect(canonicalLanguage('jpn')).toBe('jpn');
		expect(canonicalLanguage('english')).toBeNull();
		expect(languageLabel('und')).toBe('Undetermined (no language tag)');
		expect(languageLabel('xyz')).toBe('xyz');
	});
});
