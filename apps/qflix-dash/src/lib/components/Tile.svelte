<script lang="ts">
	import { onMount } from 'svelte';
	import type { TileDef, TileState } from '$lib/tiles';
	import StatusPuck from './StatusPuck.svelte';

	interface Props {
		tile: TileDef;
		puck?: TileState;
		onsupport?: () => void;
	}
	let { tile, puck = 'unknown', onsupport }: Props = $props();

	// Dynamic tiles resolve their host-specific URL client-side (e.g. Kuma's
	// per-app subdomain), so the real host never lands in committed source.
	let dyn = $state('');
	onMount(() => {
		if (tile.dynamic === 'kuma') {
			const h = location.host;
			const i = h.indexOf('.');
			if (i > 0) dyn = `https://uptimekuma-${h.slice(0, i)}.${h.slice(i + 1)}/status/public`;
		}
	});
</script>

{#if tile.action === 'support'}
	<button class="tile" onclick={() => onsupport?.()}>
		<img class="ic" class:inv={tile.invert} src={tile.icon} alt="" />
		<span class="lbl">{tile.label}</span>
		<span class="puck-slot"><StatusPuck state={puck} /></span>
	</button>
{:else}
	{@const href = tile.dynamic ? dyn || '#' : (tile.href ?? '#')}
	{@const ext = href.startsWith('http')}
	<a class="tile" href={href} target={ext ? '_blank' : null} rel={ext ? 'noreferrer' : null}>
		<img class="ic" class:inv={tile.invert} src={tile.icon} alt="" />
		<span class="lbl">{tile.label}</span>
		<span class="puck-slot"><StatusPuck state={puck} /></span>
	</a>
{/if}

<style>
	/* Inline row: icon · label · status puck (pushed right). Compact so all
	   tiles fit a phone above the fold and the usage panel hints below. */
	.tile {
		display: flex;
		flex-direction: row;
		gap: 0.85rem;
		align-items: center;
		min-height: 0;
		padding: 0.85rem 1.1rem;
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
		width: 30px;
		height: 30px;
		flex: 0 0 auto;
	}
	.ic.inv {
		filter: invert(1) brightness(1.1);
	}
	.lbl {
		font-weight: 700;
		letter-spacing: 0.02em;
		font-size: clamp(1rem, 3.6vw, 1.1rem);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	/* puck pinned to the right edge of the row */
	.puck-slot {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		flex: 0 0 auto;
	}
</style>
