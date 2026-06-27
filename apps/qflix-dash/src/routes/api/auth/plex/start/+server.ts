import { redirect } from '@sveltejs/kit';
import { createPin, authUrl } from '$lib/server/plex';
import { PIN, signPin } from '$lib/server/session';
import type { RequestHandler } from './$types';

// Kick off Sign-in-with-Plex. Build the return URL from forwarded headers so it
// works on whichever host the user started on (qflix.quadstronix.dev or the slot).
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
	redirect(302, authUrl(code, `${origin}/api/auth/plex/callback`));
};
