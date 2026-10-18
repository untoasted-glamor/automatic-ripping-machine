<script lang="ts">
	import type { SettingsGroup, ConfigFieldMeta } from '$lib/types/api.gen';
	import { saveArmConfig } from '$lib/api/settings';
	import ConfigSchemaField from './ConfigSchemaField.svelte';
	import ReviewWaitField from './ReviewWaitField.svelte';
	import { reviewWaitError, reviewWaitValue } from './review-wait';

	let {
		group,
		config,
		onsaved
	}: {
		group: SettingsGroup;
		config: Record<string, unknown>;
		onsaved?: (payload: Record<string, unknown>) => void;
	} = $props();

	const HIDDEN = '<hidden>';
	// The review-gate countdown is nullable: null = mandatory confirm (a held disc
	// never auto-starts), a number = the timed countdown. The generic text field
	// can't express that - it would PATCH "" for a cleared box and 422 - so it gets
	// a dedicated control fed from its own draft state below.
	const WAIT_KEY = 'manual_wait_seconds';
	const HOLD_KEY = 'hold_for_review';
	const editable = $derived(group.fields.filter((f: ConfigFieldMeta) => f.editable));
	const hasWaitField = $derived(editable.some((f) => f.key === WAIT_KEY));

	let values = $state<Record<string, unknown>>({});
	let mandatoryConfirm = $state(false);
	let waitDraft = $state('');
	$effect(() => {
		const next: Record<string, unknown> = {};
		for (const f of group.fields) next[f.key] = config[f.key];
		values = next;
		const wait = config[WAIT_KEY];
		mandatoryConfirm = wait == null;
		waitDraft = String(wait ?? 60);
	});

	const holdForReview = $derived(Boolean(values[HOLD_KEY]));
	const waitError = $derived(
		hasWaitField ? reviewWaitError(holdForReview, mandatoryConfirm, waitDraft) : null
	);

	let saving = $state(false);
	let feedback = $state<{ type: 'success' | 'error'; message: string } | null>(null);

	function buildPayload(): Record<string, unknown> {
		const out: Record<string, unknown> = {};
		for (const f of editable) {
			let v = values[f.key];
			if (f.key === WAIT_KEY) {
				const wait = reviewWaitValue(mandatoryConfirm, waitDraft);
				// An unparseable draft only gets here while the gate is off (Save is
				// blocked otherwise); leave the stored value alone rather than send it.
				if (wait === undefined) continue;
				v = wait;
			}
			if (f.tier === 'secret' && (v === HIDDEN || v === '' || v == null)) continue;
			if (v === config[f.key]) continue;
			out[f.key] = v;
		}
		return out;
	}

	async function save() {
		feedback = null;
		if (waitError !== null) {
			feedback = { type: 'error', message: waitError };
			return;
		}
		saving = true;
		try {
			const payload = buildPayload();
			await saveArmConfig(payload as never);
			feedback = { type: 'success', message: 'Saved' };
			onsaved?.(payload);
		} catch (e) {
			feedback = { type: 'error', message: e instanceof Error ? e.message : 'Save failed' };
		} finally {
			saving = false;
		}
	}
</script>

<section class="space-y-4">
	<h2 class="text-lg font-semibold text-gray-900 dark:text-white">{group.name}</h2>
	<div class="space-y-4">
		{#each group.fields as field (field.key)}
			{#if field.key === WAIT_KEY && field.editable}
				{#if holdForReview}
					<ReviewWaitField
						{field}
						bind:mandatory={mandatoryConfirm}
						bind:draft={waitDraft}
						error={waitError}
					/>
				{/if}
			{:else}
				<ConfigSchemaField {field} bind:value={values[field.key]} />
			{/if}
		{/each}
	</div>
	{#if editable.length > 0}
		<div class="flex items-center gap-3">
			<button onclick={save} disabled={saving || waitError !== null} class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-on-primary hover:bg-primary-hover disabled:opacity-50">
				{saving ? 'Saving...' : 'Save'}
			</button>
			{#if feedback}
				<span class="text-sm {feedback.type === 'error' ? 'text-red-600 dark:text-red-400' : 'text-gray-500'}">{feedback.message}</span>
			{/if}
		</div>
	{/if}
</section>
