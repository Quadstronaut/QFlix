// Keyboard accessibility for the support dialog. It carries aria-modal="true",
// which is a promise to keyboard users: focus moves in on open, cannot Tab out
// while open, Escape closes, and focus returns where it started. None of that
// was implemented — the only ways out were the × button and a mouse click on
// the scrim, so a keyboard-only member could never reach or dismiss it.
//
// Runs in the `client` vitest project (jsdom + resolve.conditions ['browser']).
// The *.svelte.test.ts suffix is what routes it there — see vite.config.ts.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import SupportModal from './SupportModal.svelte';

/** Open the dialog with a signed-in member, so the form controls exist. */
async function openAsMember() {
	const trigger = document.createElement('button');
	trigger.textContent = 'Support';
	document.body.appendChild(trigger);
	trigger.focus();
	expect(document.activeElement).toBe(trigger);

	const utils = render(SupportModal, { props: { open: true } });
	// Wait for /api/me to resolve and the member branch to render.
	await waitFor(() => expect(document.querySelector('textarea')).not.toBeNull());
	return { ...utils, trigger };
}

const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');

beforeEach(() => {
	vi.stubGlobal(
		'fetch',
		vi.fn(async () => ({ ok: true, json: async () => ({ member: true, name: 'Ada' }) }))
	);
	history.replaceState(null, '', '/');
});

afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
	vi.unstubAllGlobals();
});

describe('SupportModal keyboard accessibility', () => {
	it('moves focus into the dialog when opened', async () => {
		await openAsMember();
		// Containment is the actual requirement: focus must leave the page behind
		// and land inside the dialog.
		await waitFor(() => expect(dialog()!.contains(document.activeElement)).toBe(true));
		// Specifically the × button — at open time /api/me has not resolved, so the
		// form does not exist yet and × is the first control present. Deliberately
		// NOT re-focused when the member content swaps in later: moving focus out
		// from under someone mid-interaction is worse than a slightly early target.
		// classList, not an exact className match: Svelte appends a scoped-style
		// hash (e.g. "x svelte-w9n66g") that changes whenever the CSS changes.
		expect((document.activeElement as HTMLElement).classList.contains('x')).toBe(true);
		// And never the honeypot.
		expect(document.activeElement).not.toBe(dialog()!.querySelector('input.hp'));
	});

	it('closes on Escape', async () => {
		await openAsMember();
		expect(dialog()).not.toBeNull();
		await fireEvent.keyDown(window, { key: 'Escape' });
		await waitFor(() => expect(dialog()).toBeNull());
	});

	it('returns focus to the opener on close', async () => {
		const { trigger } = await openAsMember();
		await waitFor(() => expect(document.activeElement).not.toBe(trigger));
		await fireEvent.keyDown(window, { key: 'Escape' });
		await waitFor(() => expect(document.activeElement).toBe(trigger));
	});

	it('traps Tab at the end of the dialog', async () => {
		await openAsMember();
		const items = [...dialog()!.querySelectorAll<HTMLElement>('button, textarea')];
		const last = items[items.length - 1];
		last.focus();
		await fireEvent.keyDown(window, { key: 'Tab' });
		// Wraps to the first control instead of escaping to the page behind.
		await waitFor(() => expect(document.activeElement).toBe(items[0]));
	});

	it('traps Shift+Tab at the start of the dialog', async () => {
		await openAsMember();
		const items = [...dialog()!.querySelectorAll<HTMLElement>('button, textarea')];
		items[0].focus();
		await fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
		await waitFor(() => expect(document.activeElement).toBe(items[items.length - 1]));
	});

	it('keeps the honeypot out of the focus trap', async () => {
		await openAsMember();
		const hp = dialog()!.querySelector<HTMLElement>('input.hp');
		expect(hp).not.toBeNull();
		expect(hp!.getAttribute('tabindex')).toBe('-1');
		// Cycle the whole trap; the honeypot must never receive focus.
		const seen = new Set<Element | null>();
		for (let i = 0; i < 8; i++) {
			await fireEvent.keyDown(window, { key: 'Tab' });
			seen.add(document.activeElement);
		}
		expect(seen.has(hp)).toBe(false);
	});

	it('closes on a scrim click but not on a click inside the dialog', async () => {
		await openAsMember();
		await fireEvent.click(dialog()!);
		expect(dialog()).not.toBeNull(); // click inside must not close

		const scrim = document.querySelector<HTMLElement>('.scrim')!;
		await fireEvent.click(scrim);
		await waitFor(() => expect(dialog()).toBeNull());
	});
});
