<script lang="ts">
	import { onMount } from 'svelte';
	import type { Usage } from '$lib/usage';

	let usage = $state<Usage | null>(null);

	// Alphabetical by label (numeric-aware so "Sonarr 2" follows "Sonarr",
	// case-insensitive so "nginx" sorts among the N's).
	let rows = $derived(
		usage
			? [...usage.components].sort((a, b) =>
					a.label.localeCompare(b.label, undefined, { sensitivity: 'base', numeric: true })
				)
			: []
	);

	const fmt = (n: number, d = 1) => n.toFixed(d);
	const pct = (n: number) => Math.max(0, Math.min(100, n));

	async function load() {
		// Skip polling while the tab is hidden — don't run the box script for nobody.
		if (typeof document !== 'undefined' && document.hidden) return;
		try {
			const r = await fetch('/api/usage');
			if (r.ok) usage = await r.json();
			// On failure keep the last good snapshot rather than blanking the panel.
		} catch {
			/* silent — leave last snapshot in place */
		}
	}

	onMount(() => {
		load();
		const id = setInterval(load, 5000);
		return () => clearInterval(id);
	});
</script>

{#if usage}
	<section class="usage" aria-label="Live stack resource usage">
		<header class="head">
			<span class="title"><span class="pulse"></span> Live stack usage</span>
			<span class="sub">{usage.host} · {usage.ncpu} cores</span>
		</header>

		<div class="summary">
			<div class="stat">
				<span class="k">CPU</span>
				<span class="v">{fmt(usage.stack.cpu_cores, 2)} <small>cores</small></span>
				<span class="ctx">{fmt(usage.stack.cpu_pct_of_box, 2)}% of box</span>
			</div>
			<div class="stat">
				<span class="k">RAM</span>
				<span class="v">{fmt(usage.stack.ram_gib, 1)} <small>GiB</small></span>
				<span class="ctx">of {fmt(usage.stack.ram_gib_total, 0)} GiB · {fmt(usage.stack.ram_pct_of_box, 2)}%</span>
			</div>
		</div>

		<div class="legend">
			<span class="key"><span class="dot cpu"></span>CPU</span>
			<span class="key"><span class="dot ram"></span>RAM</span>
			<span class="share">share of our stack</span>
		</div>

		<ul class="rows">
			{#each rows as c (c.label)}
				<li class="comp">
					<span class="lbl" title={c.label}>{c.label}</span>
					<span class="bars">
						<span class="track"><span class="fill cpu" style="width:{pct(c.cpu_pct_of_stack)}%"></span></span>
						<span class="track"><span class="fill ram" style="width:{pct(c.ram_pct_of_stack)}%"></span></span>
					</span>
					<span class="val">{fmt(c.ram_gib, 2)} GiB</span>
				</li>
			{/each}
		</ul>
	</section>
{/if}

<style>
	.usage {
		max-width: var(--maxw);
		margin: 2.4rem auto 0;
		padding: 1.1rem 1rem 0.4rem;
		border-top: 1px solid var(--tile-border);
	}
	.head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.6rem;
		margin-bottom: 0.9rem;
	}
	.title {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		font-weight: 700;
		letter-spacing: 0.03em;
		color: var(--text);
	}
	.sub {
		color: var(--text-dim);
		font-size: 0.8rem;
		opacity: 0.7;
	}
	.pulse {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--ok);
		box-shadow: 0 0 8px var(--ok);
		animation: pulse 2s ease-in-out infinite;
	}
	@keyframes pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
	}

	.summary {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.8rem;
		margin-bottom: 1rem;
	}
	.stat {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		padding: 0.7rem 0.9rem;
		background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
		border: 1px solid var(--tile-border);
		border-radius: var(--radius);
	}
	.stat .k {
		font-size: 0.72rem;
		letter-spacing: 0.12em;
		color: var(--text-dim);
		opacity: 0.75;
	}
	.stat .v {
		font-size: 1.35rem;
		font-weight: 800;
		color: var(--accent-gold);
		line-height: 1.1;
	}
	.stat .v small {
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--text-dim);
	}
	.stat .ctx {
		font-size: 0.74rem;
		color: var(--text-dim);
		opacity: 0.7;
	}

	.legend {
		display: flex;
		align-items: center;
		gap: 0.9rem;
		font-size: 0.74rem;
		color: var(--text-dim);
		margin-bottom: 0.5rem;
	}
	.legend .key {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
	}
	.legend .share {
		margin-left: auto;
		opacity: 0.6;
	}
	.dot {
		width: 9px;
		height: 9px;
		border-radius: 2px;
		display: inline-block;
	}
	.dot.cpu,
	.fill.cpu {
		background: var(--accent-orange);
	}
	.dot.ram,
	.fill.ram {
		background: var(--accent-cyan);
	}

	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.comp {
		display: grid;
		grid-template-columns: minmax(72px, 26%) 1fr auto;
		align-items: center;
		gap: 0.7rem;
	}
	.lbl {
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.bars {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.track {
		height: 5px;
		border-radius: 3px;
		background: rgba(125, 211, 252, 0.08);
		overflow: hidden;
	}
	.fill {
		display: block;
		height: 100%;
		border-radius: 3px;
		transition: width 0.6s ease;
		min-width: 1px;
	}
	.val {
		font-size: 0.76rem;
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
</style>
