<script lang="ts">
	let { open = $bindable(false) }: { open?: boolean } = $props();

	let member = $state(false);
	let denied = $state(false);
	let name = $state('');
	let message = $state('');
	let hp = $state(''); // honeypot — bots fill it, humans never see it
	let sending = $state(false);
	let result = $state<'ok' | 'err' | ''>('');

	async function refresh() {
		denied = new URLSearchParams(location.search).get('support') === 'denied';
		member = false;
		name = '';
		try {
			const r = await fetch('/api/me');
			if (r.ok) {
				const d = await r.json();
				member = !!d.member;
				name = d.name ?? '';
			}
		} catch {
			/* silent */
		}
	}

	$effect(() => {
		if (open) {
			result = '';
			refresh();
		}
	});

	function signIn() {
		location.href = '/api/auth/plex/start';
	}

	async function submit(e: Event) {
		e.preventDefault();
		sending = true;
		result = '';
		try {
			const r = await fetch('/api/support', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ message, hp })
			});
			result = r.ok ? 'ok' : 'err';
			if (r.ok) message = '';
		} catch {
			result = 'err';
		}
		sending = false;
	}

	function close() {
		open = false;
		denied = false;
		history.replaceState(null, '', location.pathname);
	}
</script>

{#if open}
	<div class="scrim" onclick={close} role="presentation">
		<div
			class="modal"
			role="dialog"
			aria-modal="true"
			aria-label="Support"
			onclick={(e) => e.stopPropagation()}
		>
			<button class="x" onclick={close} aria-label="Close">×</button>
			<h2>Support</h2>

			{#if denied}
				<p class="msg">
					This is for QFlix members — sign in with the Plex account you watch with.
				</p>
				<button class="btn" onclick={signIn}>Try a different Plex account</button>
			{:else if !member}
				<p class="msg">Sign in with Plex to send a support request. Members only.</p>
				<button class="btn plex" onclick={signIn}>Sign in with Plex</button>
			{:else}
				{#if name}<p class="who">Signed in as {name}</p>{/if}
				<form onsubmit={submit}>
					<textarea
						bind:value={message}
						maxlength="2000"
						rows="5"
						required
						placeholder="What's up? Describe the issue or request."
					></textarea>
					<input
						class="hp"
						bind:value={hp}
						tabindex="-1"
						autocomplete="off"
						aria-hidden="true"
					/>
					<button class="btn" type="submit" disabled={sending}>
						{sending ? 'Sending…' : 'Send to QFlix'}
					</button>
				</form>
				{#if result === 'ok'}<p class="ok">Sent — thanks! We'll see it in Discord.</p>{/if}
				{#if result === 'err'}<p class="err">Couldn't send. Try again in a moment.</p>{/if}
			{/if}
		</div>
	</div>
{/if}

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: 40;
		background: rgba(5, 16, 31, 0.78);
		display: grid;
		place-items: center;
		padding: 1rem;
	}
	.modal {
		position: relative;
		width: min(440px, 100%);
		background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
		border: 1px solid var(--accent-cyan);
		border-radius: var(--radius);
		box-shadow: 0 18px 60px rgba(0, 0, 0, 0.55);
		padding: 1.4rem 1.4rem 1.6rem;
	}
	h2 {
		margin: 0 0 0.8rem;
		letter-spacing: 0.06em;
		color: var(--text);
	}
	.x {
		position: absolute;
		top: 0.5rem;
		right: 0.6rem;
		background: none;
		border: 0;
		color: var(--text-dim);
		font-size: 1.4rem;
		cursor: pointer;
	}
	.msg,
	.who {
		color: var(--text-dim);
		margin: 0 0 1rem;
	}
	.who {
		font-size: 0.85rem;
		color: var(--accent-cyan);
	}
	textarea {
		width: 100%;
		resize: vertical;
		background: var(--bg-0);
		color: var(--text);
		border: 1px solid var(--tile-border);
		border-radius: var(--radius);
		padding: 0.6rem;
		margin-bottom: 0.8rem;
	}
	.btn {
		width: 100%;
		padding: 0.7rem 1rem;
		background: var(--accent-orange);
		color: #1a0f04;
		font-weight: 800;
		letter-spacing: 0.03em;
		border: 0;
		border-radius: var(--radius);
		cursor: pointer;
	}
	.btn.plex {
		background: #e5a00d;
	}
	.btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.hp {
		position: absolute;
		left: -9999px;
		width: 1px;
		height: 1px;
		opacity: 0;
	}
	.ok {
		color: var(--ok);
		margin: 0.8rem 0 0;
	}
	.err {
		color: var(--down);
		margin: 0.8rem 0 0;
	}
</style>
