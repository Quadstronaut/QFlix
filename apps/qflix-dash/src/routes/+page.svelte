<script lang="ts">
	import { onMount } from 'svelte';
	import Hero from '$lib/components/Hero.svelte';
	import Board from '$lib/components/Board.svelte';
	import SupportModal from '$lib/components/SupportModal.svelte';
	import type { TileState } from '$lib/tiles';

	let states = $state<Record<string, TileState>>({});
	let greeting = $state('');
	let supportOpen = $state(false);

	onMount(async () => {
		// live status dots — silent fail leaves pucks 'unknown'
		try {
			const r = await fetch('/api/status');
			if (r.ok) states = await r.json();
		} catch {
			/* silent */
		}
		// silent greeting — silent check / silent fail / silent on empty
		try {
			const r = await fetch('/api/me');
			if (r.ok) {
				const d = await r.json();
				if (d?.name) greeting = `Now showing for ${d.name}`;
			}
		} catch {
			/* silent */
		}
		// returning from Plex auth opens the support flow
		if (new URLSearchParams(location.search).get('support')) supportOpen = true;
	});
</script>

<main>
	<Hero {greeting} />
	<Board {states} onsupport={() => (supportOpen = true)} />
</main>

<SupportModal bind:open={supportOpen} />

<style>
	main {
		position: relative;
		z-index: 1;
		padding: 2rem 0 4rem;
	}
</style>
