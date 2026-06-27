import { expect, test, vi, beforeEach } from 'vitest';

beforeEach(() => {
	vi.restoreAllMocks();
	process.env.PLEX_CLIENT_ID = 'cid-123';
});

test('authUrl carries clientID, code, and an encoded forwardUrl', async () => {
	const { authUrl } = await import('./plex');
	const u = authUrl('ABCD', 'https://x.dev/api/auth/plex/callback');
	expect(u).toContain('app.plex.tv/auth#?');
	expect(u).toContain('clientID=cid-123');
	expect(u).toContain('code=ABCD');
	expect(u).toContain('forwardUrl=https%3A%2F%2Fx.dev%2Fapi%2Fauth%2Fplex%2Fcallback');
});

test('createPin posts + parses; whoami parses identity (email lowercased)', async () => {
	const { createPin, whoami } = await import('./plex');
	const f = vi
		.fn()
		.mockResolvedValueOnce({ ok: true, json: async () => ({ id: 7, code: 'ZZ' }) })
		.mockResolvedValueOnce({ ok: true, json: async () => ({ id: 42, username: 'kyle', email: 'K@X.com' }) });
	vi.stubGlobal('fetch', f);
	expect(await createPin()).toEqual({ id: 7, code: 'ZZ' });
	expect(await whoami('tok')).toEqual({ id: 42, username: 'kyle', email: 'k@x.com' });
});
