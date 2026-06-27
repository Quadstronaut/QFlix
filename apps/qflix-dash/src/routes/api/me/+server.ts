import { json } from '@sveltejs/kit';
import { AUTH, GREET, verifyAuth, verifyGreeting } from '$lib/server/session';
import type { RequestHandler } from './$types';

// Silent identity read for the greeting + Support modal. Never errors to the
// client, never triggers a login. {name?, member?} or {}.
export const GET: RequestHandler = ({ cookies }) => {
	const out: { name?: string; member?: boolean } = {};
	try {
		const g = verifyGreeting(cookies.get(GREET));
		if (g) out.name = g.n;
		const a = verifyAuth(cookies.get(AUTH));
		if (a) out.member = true;
	} catch {
		/* silent — return whatever we have */
	}
	return json(out);
};
