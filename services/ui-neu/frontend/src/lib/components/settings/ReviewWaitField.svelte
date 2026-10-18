<script lang="ts">
	import ChoiceCard from '$lib/components/ChoiceCard.svelte';
	import { reviewWaitError, waitSecondsOrDefault } from './review-wait';

	// The review gate's two sub-modes, over two config keys
	// (review_requires_confirm + manual_wait_seconds): wait for the operator's
	// confirm, or start the rip after a countdown. Rendered by SchemaConfigForm
	// for the `review_wait` widget in Settings > Ripping > Review gate. `values`
	// is the live form state it writes into; `context` the saved config.
	let { values, context = {} }: { values: Record<string, unknown>; context?: Record<string, unknown> } = $props();

	const current = $derived(values.review_requires_confirm === true ? 'confirm' : 'countdown');
	const holdOn = $derived((values.hold_for_review ?? context.hold_for_review) === true);
	const waitError = $derived(current === 'countdown' ? reviewWaitError(values.manual_wait_seconds) : null);

	const options = [
		{
			value: 'confirm',
			title: 'Require manual confirm (no auto-start)',
			description: 'A held disc waits until you press Start rip. It never starts on its own.'
		},
		{
			value: 'countdown',
			title: 'Start after a countdown',
			description: 'A held disc rips by itself when the countdown ends, unless you pause it.'
		}
	];

	function choose(v: string) {
		values.review_requires_confirm = v === 'confirm';
		// The countdown is still saved while hidden: keep it a value the backend
		// accepts, so a half-typed box can't make Save fail in confirm mode.
		if (reviewWaitError(values.manual_wait_seconds) !== null) {
			values.manual_wait_seconds = waitSecondsOrDefault(context.manual_wait_seconds);
		}
	}

	function onWaitInput(e: Event) {
		const raw = (e.currentTarget as HTMLInputElement).value;
		values.manual_wait_seconds = raw === '' ? null : Number(raw);
	}
</script>

<div class="review-wait-field stack" data-testid="review-wait-field">
	<div>
		<div class="field-label review-wait-field-title">While a disc is held</div>
		<p class="field-help">
			{holdOn
				? 'How a disc held for review gets to the rip.'
				: 'Applies when "When you insert a disc" is set to Review first.'}
		</p>
	</div>
	<ChoiceCard name="review-wait" value={current} {options} onchange={choose} legend="While a disc is held" />
	{#if current === 'countdown'}
		<label class="stack review-wait-field-countdown">
			<span class="field-label">Review countdown (seconds)</span>
			<input
				type="number"
				step="1"
				min="0"
				inputmode="numeric"
				aria-label="Review countdown (seconds)"
				aria-invalid={waitError !== null}
				data-testid="manual-wait-seconds"
				value={values.manual_wait_seconds ?? ''}
				oninput={onWaitInput}
				class="field-control review-wait-field-number"
			/>
		</label>
		{#if waitError}
			<p class="field-help review-wait-field-error" data-testid="manual-wait-seconds-error">{waitError}</p>
		{:else}
			<p class="field-help">0 starts the rip as soon as the disc is identified.</p>
		{/if}
	{/if}
</div>

<style>
	.review-wait-field {
		gap: 0.75rem;
	}
	.review-wait-field-title {
		color: var(--color-text);
		font-weight: 600;
	}
	.review-wait-field-countdown {
		gap: 0.25rem;
	}
	/* a short number, not a full-width text box (same as ConfigSchemaField's int). */
	.review-wait-field-number {
		width: 8rem;
	}
	.review-wait-field-error {
		color: var(--color-danger);
	}
</style>
