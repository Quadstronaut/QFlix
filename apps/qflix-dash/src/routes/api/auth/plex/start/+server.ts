import { redirect } from '@sveltejs/kit';
import { createPin, authUrl } from '#lib/server/plex.js';
import { PIN, signPin } from '#lib/server/session.js';
import type { RequestHandler } from './$types';

// Kick off Sign-in-with-Plex. Build the return URL from forwarded headers so it
// works on whichever host the user started on (the public dash vhost, e.g. qflix.example.com, or the slot at seedbox.example.com).
export const GET: RequestHandler = async ({ url, request, cookies }) => {
	const proto = request.headers.get('x-forwarded-proto') ?? url.protocol.replace(':', '');
	const host = request.headers.get('x-forwarded-host') ?? url.host;
	const origin = `${proto}://${host}`;

	const { id, code } = await createPin();
	cookies.set(PIN, signPin(id), {
		path: '/',
		httpOnly: true,
		secure: true,
		sameSite: 'lax',
		maxAge: 600
	});
	// SvelteKit 3 refuses external redirects unless opted in; allow exactly Plex's
	// auth origin (authUrl always builds https://app.plex.tv/auth#?...).
	redirect(302, authUrl(code, `${origin}/api/auth/plex/callback`), {
		external: ['https://app.plex.tv']
	});
};
