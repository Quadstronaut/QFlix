<script lang="ts">
	import type { TileState } from '$lib/tiles';
	interface Props {
		state?: TileState;
	}
	let { state = 'unknown' }: Props = $props();
	const label: Record<TileState, string> = {
		ok: 'Online',
		warn: 'Degraded',
		down: 'Down',
		unknown: 'Status unknown'
	};
</script>

<span class="puck" role="img" data-state={state} aria-label={label[state]}></span>

<style>
	.puck {
		width: 10px;
		height: 10px;
		border-radius: 50%;
		display: inline-block;
		flex: none;
	}
	[data-state='ok'] {
		background: var(--ok);
		box-shadow: 0 0 8px var(--ok);
	}
	[data-state='warn'] {
		background: var(--warn);
		box-shadow: 0 0 8px var(--warn);
	}
	[data-state='down'] {
		background: var(--down);
		box-shadow: 0 0 8px var(--down);
	}
	[data-state='unknown'] {
		background: var(--unknown);
	}
</style>
