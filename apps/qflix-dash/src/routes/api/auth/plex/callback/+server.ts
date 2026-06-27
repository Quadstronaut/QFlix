import { redirect } from '@sveltejs/kit';
import { pollPin, whoami } from '$lib/server/plex';
import { isMember } from '$lib/server/membership';
import { AUTH, GREET, PIN, verifyPin, signAuth, signGreeting } from '$lib/server/session';
import type { RequestHandler } from './$types';

const COOKIE = { path: '/', httpOnly: true, secure: true, sameSite: 'lax' } as const;

export const GET: RequestHandler = async ({ cookies }) => {
	const pin = verifyPin(cookies.get(PIN));
	cookies.delete(PIN, { path: '/' });
	if (!pin) redirect(302, '/?support=denied');

	// The user just authorized at app.plex.tv, so the token is usually ready
	// immediately; poll briefly as a safety margin.
	let token: string | null = null;
	for (let i = 0; i < 25 && !token; i++) {
		token = await pollPin(pin.pin);
		if (!token) await new Promise((r) => setTimeout(r, 1000));
	}
	if (!token) redirect(302, '/?support=denied');

	const who = await whoami(token);
	const matched = await isMember({ id: who.id, email: who.email });
	if (!matched) redirect(302, '/?support=denied');

	cookies.set(AUTH, signAuth({ u: who.username, e: who.email }), { ...COOKIE, maxAge: 1800 });
	cookies.set(GREET, signGreeting(who.username || who.email), { ...COOKIE, maxAge: 2592000 });
	redirect(302, '/?support=1');
};
