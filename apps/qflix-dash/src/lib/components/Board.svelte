<script lang="ts">
	import { TILES } from '$lib/tiles';
	import type { TileState } from '$lib/tiles';
	import Tile from './Tile.svelte';

	interface Props {
		states?: Record<string, TileState>;
		onsupport?: () => void;
	}
	let { states = {}, onsupport }: Props = $props();
</script>

<section class="grid">
	{#each TILES as t (t.key)}
		<Tile tile={t} state={states[t.statusKey] ?? 'unknown'} {onsupport} />
	{/each}
</section>

<style>
	.grid {
		display: grid;
		gap: 0.9rem;
		max-width: var(--maxw);
		margin: 1.6rem auto;
		padding: 0 1rem;
		grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
	}
	/* single column on phones — no clipped labels, big tap targets */
	@media (max-width: 520px) {
		.grid {
			grid-template-columns: 1fr;
		}
	}
</style>
