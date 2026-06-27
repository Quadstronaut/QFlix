// Plex PIN OAuth ("Sign in with Plex"), server-side. The user's token proves
// identity, then is discarded — never stored.
import { cfg } from './env';

const BASE = 'https://plex.tv/api/v2';

function headers(extra: Record<string, string> = {}): Record<string, string> {
	const c = cfg();
	return {
		accept: 'application/json',
		'X-Plex-Product': 'QFlix',
		'X-Plex-Client-Identifier': c.plexClientId,
		...extra
	};
}

export async function createPin(): Promise<{ id: number; code: string }> {
	const r = await fetch(`${BASE}/pins?strong=true`, { method: 'POST', headers: headers() });
	if (!r.ok) throw new Error(`plex pin create failed: ${r.status}`);
	const j = await r.json();
	return { id: j.id, code: j.code };
}

export function authUrl(code: string, forwardUrl: string): string {
	const c = cfg();
	const params = new URLSearchParams({
		clientID: c.plexClientId,
		code,
		'context[device][product]': 'QFlix',
		forwardUrl
	});
	return `https://app.plex.tv/auth#?${params.toString()}`;
}

export async function pollPin(id: number): Promise<string | null> {
	const r = await fetch(`${BASE}/pins/${id}`, { headers: headers() });
	if (!r.ok) return null;
	const j = await r.json();
	return j.authToken || null;
}

export async function whoami(authToken: string): Promise<{ id: number; username: string; email: string }> {
	const r = await fetch(`${BASE}/user`, { headers: headers({ 'X-Plex-Token': authToken }) });
	if (!r.ok) throw new Error(`plex whoami failed: ${r.status}`);
	const j = await r.json();
	return { id: j.id, username: j.username || j.title || '', email: (j.email || '').toLowerCase() };
}
