<script lang="ts">
	// Season, disc number and disc total: shared by the Identity panel and IdentifyDialog.
	let {
		season = $bindable(null),
		discNumber = $bindable(null),
		discTotal = $bindable(null),
		isCd = false,
		disabled = false
	}: {
		season?: number | null;
		discNumber?: number | null;
		discTotal?: number | null;
		isCd?: boolean;
		disabled?: boolean;
	} = $props();
</script>

<div class="mb-1 flex gap-3">
	{#if !isCd}
		<label class="field flex-1">
			<span class="field-label">Season</span>
			<input bind:value={season} type="number" min="0" max="999" data-testid="identify-season" {disabled} />
		</label>
	{/if}
	<label class="field flex-1">
		<span class="field-label">Disc #</span>
		<input bind:value={discNumber} type="number" min="1" max="999" data-testid="identify-disc-number" {disabled} />
	</label>
	<label class="field flex-1">
		<span class="field-label">Disc total</span>
		<input bind:value={discTotal} type="number" min="1" max="999" data-testid="identify-disc-total" {disabled} />
	</label>
</div>
<p class="field-help mb-3">
	{#if isCd}
		Multi-disc sets only: fills the <code class="mono">{'{disc}'}</code> naming token. Leave blank for a single disc.
	{:else}
		TV box sets and multi-disc releases: fills the <code class="mono">{'{season}'}</code> and
		<code class="mono">{'{disc}'}</code> naming tokens. Leave blank for a single disc.
	{/if}
</p>
