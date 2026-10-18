<script lang="ts">
	import type { ConfigFieldMeta } from '$lib/types/api.gen';
	import { DEFAULT_WAIT_SECONDS } from './review-wait';

	// The review-gate countdown control: "Require manual confirm" (wire value
	// null, the disc waits for an explicit Start) or a countdown in seconds.
	let {
		field,
		mandatory = $bindable(false),
		draft = $bindable(''),
		error = null
	}: {
		field: ConfigFieldMeta;
		mandatory: boolean;
		draft: string;
		error?: string | null;
	} = $props();

	function onMandatoryChange(e: Event) {
		mandatory = (e.currentTarget as HTMLInputElement).checked;
		// Leaving mandatory mode with an empty box restores a sane default
		// rather than immediately flagging the field as invalid.
		if (!mandatory && draft.trim() === '') draft = String(DEFAULT_WAIT_SECONDS);
	}

	const inputClass =
		'w-32 rounded-lg border border-primary/25 bg-primary/5 px-3 py-2 text-sm tabular-nums dark:border-primary/30 dark:bg-primary/10 dark:text-white';
</script>

<div class="space-y-2 border-l-2 border-primary/20 pl-4" data-testid="review-wait-field">
	<label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
		<input
			type="checkbox"
			data-testid="mandatory-confirm"
			checked={mandatory}
			onchange={onMandatoryChange}
		/>
		<span class="font-medium">Require manual confirm (no auto-start)</span>
	</label>
	{#if !mandatory}
		<label class="block space-y-1">
			<span class="block text-sm font-medium text-gray-700 dark:text-gray-300">{field.label}</span>
			<input
				type="text"
				inputmode="numeric"
				aria-label={field.label}
				aria-invalid={error !== null}
				data-testid="manual-wait-seconds"
				value={draft}
				oninput={(e) => (draft = (e.currentTarget as HTMLInputElement).value)}
				class={inputClass}
			/>
		</label>
		{#if error}
			<p class="text-xs text-red-600 dark:text-red-400" data-testid="manual-wait-seconds-error">{error}</p>
		{/if}
	{:else}
		<p class="text-xs text-gray-500 dark:text-gray-400">
			Held discs wait on the dashboard until you click Start rip.
		</p>
	{/if}
	{#if field.help}
		<p class="text-xs text-gray-500 dark:text-gray-400">{field.help}</p>
	{/if}
</div>
