<script lang="ts">
	import type { TileDef, TileState } from '$lib/tiles';
	import StatusPuck from './StatusPuck.svelte';

	interface Props {
		tile: TileDef;
		state?: TileState;
		onsupport?: () => void;
	}
	let { tile, state = 'unknown', onsupport }: Props = $props();
	const ext = tile.href?.startsWith('http') ?? false;
</script>

{#if tile.action === 'support'}
	<button class="tile" onclick={() => onsupport?.()}>
		<img class="ic" class:inv={tile.invert} src={tile.icon} alt="" />
		<span class="row"><span class="lbl">{tile.label}</span><StatusPuck {state} /></span>
	</button>
{:else}
	<a class="tile" href={tile.href} target={ext ? '_blank' : null} rel={ext ? 'noreferrer' : null}>
		<img class="ic" class:inv={tile.invert} src={tile.icon} alt="" />
		<span class="row"><span class="lbl">{tile.label}</span><StatusPuck {state} /></span>
	</a>
{/if}

<style>
	.tile {
		display: flex;
		flex-direction: column;
		gap: 0.7rem;
		align-items: flex-start;
		justify-content: space-between;
		min-height: 124px;
		padding: 1rem;
		background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
		border: 1px solid var(--tile-border);
		border-radius: var(--radius);
		color: var(--text);
		width: 100%;
		text-align: left;
		cursor: pointer;
		transition:
			transform 0.12s,
			border-color 0.12s,
			box-shadow 0.12s;
	}
	.tile:hover,
	.tile:focus-visible {
		transform: translateY(-3px);
		border-color: var(--accent-cyan);
		box-shadow: 0 6px 22px rgba(125, 211, 252, 0.18);
		outline: none;
	}
	.ic {
		width: 34px;
		height: 34px;
	}
	.ic.inv {
		filter: invert(1) brightness(1.1);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		justify-content: space-between;
	}
	.lbl {
		font-weight: 700;
		letter-spacing: 0.02em;
		white-space: normal;
		overflow-wrap: anywhere;
		font-size: clamp(0.95rem, 3.6vw, 1.15rem);
	}
</style>
