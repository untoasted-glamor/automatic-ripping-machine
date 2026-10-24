<script lang="ts">
	// An ordered list of ISO 639-2 language codes: pick a common language or
	// type any code, reorder with the arrows, remove with the cross. Controlled
	// like TrackFiltersEditor: `value` in, `onchange` out.
	import Glyph from './Glyph.svelte';
	import { COMMON_LANGUAGES, canonicalLanguage, languageLabel } from '$lib/languages';

	interface Props {
		value: string[];
		onchange: (v: string[]) => void;
		label: string;
		testid: string;
		disabled?: boolean;
		// Shown under the first entry, e.g. "default track".
		firstNote?: string;
	}

	let { value, onchange, label, testid, disabled = false, firstNote }: Props = $props();

	let custom = $state('');
	let error = $state<string | null>(null);

	const available = $derived(COMMON_LANGUAGES.filter((l) => !value.includes(l.code)));

	function add(raw: string): void {
		const code = canonicalLanguage(raw);
		if (code === null) {
			error = `"${raw.trim()}" is not an ISO 639-2 code (three letters, e.g. fin).`;
			return;
		}
		error = null;
		if (!value.includes(code)) onchange([...value, code]);
	}

	function addCustom(): void {
		if (!custom.trim()) return;
		add(custom);
		if (error === null) custom = '';
	}

	function onCustomKey(event: KeyboardEvent): void {
		// Enter adds the code instead of submitting the preset form.
		if (event.key === 'Enter') {
			event.preventDefault();
			addCustom();
		}
	}

	function onPick(event: Event & { currentTarget: HTMLSelectElement }): void {
		const code = event.currentTarget.value;
		event.currentTarget.value = '';
		if (code) add(code);
	}

	function move(index: number, delta: number): void {
		const next = [...value];
		const [item] = next.splice(index, 1);
		next.splice(index + delta, 0, item);
		onchange(next);
	}

	function remove(index: number): void {
		onchange(value.filter((_, i) => i !== index));
	}
</script>

<div class="language-list" data-testid={testid}>
	<span class="field-label" id="{testid}-label">{label}</span>
	{#if value.length === 0}
		<p class="field-help" data-testid="{testid}-empty">No languages added yet.</p>
	{:else}
		<ol class="language-list-items" aria-labelledby="{testid}-label">
			{#each value as code, i (code)}
				<li class="list-row list-row-compact language-list-row" data-testid="{testid}-item">
					<div class="list-row-main">
						<span>{languageLabel(code)}</span>
						<span>{code}{i === 0 && firstNote ? ` | ${firstNote}` : ''}</span>
					</div>
					<div class="list-row-actions">
						<button
							type="button"
							class="btn btn-icon btn-sm"
							aria-label="Move {languageLabel(code)} up"
							disabled={disabled || i === 0}
							onclick={() => move(i, -1)}
						>
							<Glyph name="chevron-up" />
						</button>
						<button
							type="button"
							class="btn btn-icon btn-sm"
							aria-label="Move {languageLabel(code)} down"
							disabled={disabled || i === value.length - 1}
							onclick={() => move(i, 1)}
						>
							<Glyph name="chevron-down" />
						</button>
						<button
							type="button"
							class="btn btn-icon btn-sm"
							aria-label="Remove {languageLabel(code)}"
							{disabled}
							onclick={() => remove(i)}
						>
							<Glyph name="x" />
						</button>
					</div>
				</li>
			{/each}
		</ol>
	{/if}

	{#if !disabled}
		<div class="language-list-add">
			<select class="field-control" aria-label="Add a common language" data-testid="{testid}-pick" onchange={onPick}>
				<option value="">Add a language...</option>
				{#each available as lang (lang.code)}
					<option value={lang.code}>{lang.name} ({lang.code})</option>
				{/each}
			</select>
			<input
				class="field-control language-list-code"
				type="text"
				maxlength="3"
				placeholder="Other code"
				aria-label="ISO 639-2 language code"
				data-testid="{testid}-code"
				bind:value={custom}
				onkeydown={onCustomKey}
				aria-invalid={error ? 'true' : undefined}
			/>
			<button type="button" class="btn btn-sm" data-testid="{testid}-add" onclick={addCustom}>Add</button>
		</div>
		{#if error}
			<p class="field-error" data-testid="{testid}-error">{error}</p>
		{/if}
	{/if}
</div>

<style>
	.language-list {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}
	.language-list-items {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
	}
	.language-list-row {
		grid-template-columns: minmax(0, 1fr) auto;
		cursor: default;
	}
	.language-list-add {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: center;
	}
	.language-list-add select {
		flex: 1 1 12rem;
		width: auto;
	}
	.language-list-code {
		flex: 0 0 7rem;
		width: 7rem;
	}
</style>
