<script lang="ts">
	import { onMount } from 'svelte';
	import ChoiceCard from '$lib/components/ChoiceCard.svelte';
	import StatusStrip from '$lib/components/StatusStrip.svelte';
	import { fetchDrives } from '$lib/api/drives';
	import { dashboard } from '$lib/stores/dashboard';
	import { PAUSED_LABEL } from '$lib/utils/pause';
	import { reviewWaitError, waitSecondsOrDefault } from './review-wait';

	// "When you insert a disc": one three-way choice over two config keys
	// (auto_rip_on_insert + hold_for_review), and under "Review first" how a
	// held disc gets to the rip (review_requires_confirm + manual_wait_seconds).
	// The global default each drive follows unless it has its own mode (setup
	// spec D1). Rendered by SchemaConfigForm for the `disc_handling` widget, in
	// Settings > Ripping and in the setup walkthrough. `values` is the live form
	// state it writes into; `context` the saved config.
	let { values, context = {} }: { values: Record<string, unknown>; context?: Record<string, unknown> } = $props();

	// Live pause state once the dashboard poll has answered (the header toggle
	// can pause while this page is open), else the saved config. Pause holds
	// every new disc for review, so the choice is locked until Resume.
	const dashboardReady = dashboard.initialized;
	const paused = $derived(
		$dashboardReady && $dashboard.db_available ? !$dashboard.ripping_enabled : context.ripping_paused === true
	);

	const waitSeconds = $derived(waitSecondsOrDefault(values.manual_wait_seconds, context.manual_wait_seconds));
	const requiresConfirm = $derived((values.review_requires_confirm ?? context.review_requires_confirm) === true);
	const current = $derived(
		values.auto_rip_on_insert === false ? 'manual' : values.hold_for_review === true ? 'review' : 'auto'
	);
	const reviewMode = $derived(requiresConfirm ? 'confirm' : 'countdown');
	const waitError = $derived(
		current === 'review' && reviewMode === 'countdown' ? reviewWaitError(values.manual_wait_seconds) : null
	);

	const options = $derived([
		{ value: 'auto', title: 'Fully automatic', description: 'Insert a disc and ARM identifies, rips and files it.' },
		{
			value: 'review',
			title: 'Review first',
			badge: 'Suggested',
			description: requiresConfirm
				? 'ARM identifies the disc, then waits for you to check the title and press Start rip. Nothing rips until you do.'
				: `ARM identifies the disc, then waits ${waitSeconds} seconds for you to check the title before ripping. No answer and it rips anyway.`
		},
		{ value: 'manual', title: 'Manual', description: 'Nothing starts until you press Start.' }
	]);
	const reviewOptions = [
		{
			value: 'countdown',
			title: 'Start after a countdown',
			description: 'A held disc rips by itself when the countdown ends, unless you pause it.'
		},
		{
			value: 'confirm',
			title: 'Hold until I confirm',
			description: 'A held disc waits until you press Start rip. It never starts on its own.'
		}
	];

	// The countdown is saved even while its box is hidden: keep it a value the
	// backend accepts, so a half-typed box can't make Save fail.
	function keepWaitValid() {
		if (reviewWaitError(values.manual_wait_seconds) !== null) {
			values.manual_wait_seconds = waitSecondsOrDefault(context.manual_wait_seconds);
		}
	}

	function choose(v: string) {
		if (v !== 'review') keepWaitValid();
		if (v === 'manual') {
			values.auto_rip_on_insert = false;
			return;
		}
		values.auto_rip_on_insert = true;
		values.hold_for_review = v === 'review';
	}

	function chooseReview(v: string) {
		values.review_requires_confirm = v === 'confirm';
		if (v === 'confirm') keepWaitValid();
	}

	function onWaitInput(e: Event) {
		const raw = (e.currentTarget as HTMLInputElement).value;
		values.manual_wait_seconds = raw === '' ? null : Number(raw);
	}

	let overrides = $state(0);
	onMount(async () => {
		try {
			overrides = (await fetchDrives()).filter((d) => d.lifecycle === 'enrolled' && d.drive_mode != null).length;
		} catch {
			overrides = 0;
		}
	});
</script>

<div class="disc-handling-field stack">
	<div>
		<div class="field-label disc-handling-field-title">When you insert a disc</div>
		<p class="field-help">Applies to every drive. You can change it per drive later.</p>
	</div>
	{#if paused}
		<div data-testid="disc-handling-paused">
			<StatusStrip tone="warning" title={PAUSED_LABEL} />
		</div>
	{/if}
	<ChoiceCard
		name="disc-handling"
		value={current}
		{options}
		onchange={choose}
		disabled={paused}
		legend="When you insert a disc"
	/>
	{#if current === 'review'}
		<div class="stack disc-handling-field-review" data-testid="disc-handling-review">
			<div class="field-label disc-handling-field-subtitle">While a disc is held</div>
			<ChoiceCard
				name="disc-handling-review"
				value={reviewMode}
				options={reviewOptions}
				onchange={chooseReview}
				disabled={paused}
				legend="While a disc is held"
			/>
			{#if reviewMode === 'countdown'}
				<label class="stack disc-handling-field-countdown">
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
						disabled={paused}
						class="field-control disc-handling-field-number"
					/>
				</label>
				{#if waitError}
					<p class="field-help disc-handling-field-error" data-testid="manual-wait-seconds-error">{waitError}</p>
				{:else}
					<p class="field-help">0 starts the rip as soon as the disc is identified.</p>
				{/if}
			{/if}
		</div>
	{/if}
	{#if overrides > 0}
		<p class="field-help" data-testid="disc-handling-overrides">
			{overrides === 1 ? '1 drive has its own setting and' : `${overrides} drives have their own setting and`} won't change.
		</p>
	{/if}
</div>

<style>
	.disc-handling-field {
		gap: 0.75rem;
	}
	.disc-handling-field-title {
		color: var(--color-text);
		font-weight: 600;
	}
	/* the Review first sub-choice, indented under its card group */
	.disc-handling-field-review {
		gap: 0.75rem;
		border-left: 2px solid var(--color-border);
		padding-left: 0.875rem;
	}
	.disc-handling-field-subtitle {
		color: var(--color-text);
		font-weight: 500;
	}
	.disc-handling-field-countdown {
		gap: 0.25rem;
	}
	/* a short number, not a full-width text box (same as ConfigSchemaField's int). */
	.disc-handling-field-number {
		width: 8rem;
	}
	.disc-handling-field-error {
		color: var(--color-danger);
	}
</style>
