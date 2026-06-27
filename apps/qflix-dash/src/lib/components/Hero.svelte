<script lang="ts">
	interface Props {
		greeting?: string;
	}
	let { greeting = '' }: Props = $props();
</script>

<header class="hero spotlight">
	<img class="q" src="/q.png" alt="QFlix" width="76" height="76" />
	<div class="word">
		<span class="title">QFlix</span>
		<span class="rule"></span>
	</div>
	<!-- greeting slot: reserved height so a fade-in never shifts layout -->
	<span class="greet" class:show={!!greeting} id="greeting">{greeting}</span>
</header>

<style>
	.hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.6rem;
		max-width: var(--maxw);
		margin: 0 auto;
		padding: 1.5rem 1rem 0;
		text-align: center;
		position: relative;
		z-index: 1;
	}
	.q {
		filter: drop-shadow(0 0 18px rgba(125, 211, 252, 0.45));
		border-radius: 50%;
		position: relative;
		z-index: 1;
	}
	.word {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.4rem;
	}
	.title {
		font-size: clamp(2rem, 9vw, 3.2rem);
		font-weight: 800;
		letter-spacing: 0.14em;
		background: linear-gradient(90deg, var(--accent-cyan), #fff 42%, var(--accent-gold));
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	.rule {
		width: 120px;
		height: 2px;
		background: linear-gradient(90deg, transparent, var(--accent-orange), transparent);
	}
	.greet {
		min-height: 1.4em;
		color: var(--accent-cyan);
		font-weight: 600;
		letter-spacing: 0.04em;
		opacity: 0;
		transform: translateY(4px);
		transition:
			opacity 0.5s,
			transform 0.5s;
	}
	.greet.show {
		opacity: 1;
		transform: none;
	}
</style>
