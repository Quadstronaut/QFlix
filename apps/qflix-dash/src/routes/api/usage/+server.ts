import { json } from '@sveltejs/kit';
import { getUsage } from '$lib/server/usage';
import type { RequestHandler } from './$types';

// Live stack-usage feed for the dashboard panel. Never blocks the page: on any
// failure (bin unset, script error, parse error) it returns 503 so the client
// quietly hides the panel rather than surfacing an error.
export const GET: RequestHandler = async () => {
	try {
		return json(await getUsage());
	} catch {
		return json({ unavailable: true }, { status: 503 });
	}
};
