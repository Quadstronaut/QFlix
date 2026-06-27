import { AUTH, verifyAuth } from '$lib/server/session';
import { allow } from '$lib/server/ratelimit';
import { cfg } from '$lib/server/env';
import { validateMessage, buildWebhookPayload } from '$lib/server/support';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ request, cookies }) => {
	const who = verifyAuth(cookies.get(AUTH));
	if (!who) return new Response('unauthorized', { status: 401 });

	let body: { message?: unknown; hp?: unknown };
	try {
		body = await request.json();
	} catch {
		return new Response('bad request', { status: 400 });
	}

	// honeypot — bots fill it; accept-and-drop so they get no signal
	if (body.hp) return new Response(null, { status: 204 });

	const message = validateMessage(body.message);
	if (!message) return new Response('bad request', { status: 400 });

	if (!allow(`sup:${who.e}`, 3, 3_600_000)) return new Response('rate limited', { status: 429 });

	const c = cfg();
	if (!c.discordWebhook) return new Response('misconfigured', { status: 500 });

	const payload = buildWebhookPayload(who, message, c.qAvatar, new Date().toISOString());
	await fetch(c.discordWebhook, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(payload)
	});
	return new Response(null, { status: 204 });
};
