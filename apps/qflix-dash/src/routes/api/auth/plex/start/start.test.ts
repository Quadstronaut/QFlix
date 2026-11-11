// QFLX-93: SvelteKit 3 throws on an external redirect() unless it is opted in.
// Sign-in-with-Plex redirects to app.plex.tv, so without the allowlist the
// route 500s instead of starting the sign-in. Guard that it still redirects.
import { expect, test, vi } from 'vitest';
import { isRedirect, redirect } from '@sveltejs/kit';

test('start redirects to app.plex.tv (external redirect allowlisted)', async () => {
	process.env.SESSION_SECRET = 'test-secret';
	process.env.PLEX_CLIENT_ID = 'cid-123';
	vi.stubGlobal(
		'fetch',
		vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 7, code: 'ZZ' }) })
	);
	const { GET } = await import('./+server.ts');
	const cookies = { set: vi.fn() };
	let thrown: unknown;
	try {
		await GET({
			url: new URL('http://127.0.0.1:42020/api/auth/plex/start'),
			request: new Request('http://127.0.0.1:42020/api/auth/plex/start', {
				headers: { 'x-forwarded-proto': 'https', 'x-forwarded-host': 'dash.example.com' }
			}),
			cookies
		} as never);
	} catch (e) {
		thrown = e;
	}
	expect(isRedirect(thrown)).toBe(true);
	const r = thrown as { status: number; location: string };
	expect(r.status).toBe(302);
	expect(r.location.startsWith('https://app.plex.tv/auth#?')).toBe(true);
	expect(r.location).toContain(
		'forwardUrl=https%3A%2F%2Fdash.example.com%2Fapi%2Fauth%2Fplex%2Fcallback'
	);
	expect(cookies.set).toHaveBeenCalledOnce();
	vi.unstubAllGlobals();
});

test('control: kit 3 really does refuse an un-allowlisted external redirect', () => {
	expect(() => redirect(302, 'https://app.plex.tv/auth')).toThrow();
	expect(() => redirect(302, 'https://evil.example/', { external: ['https://app.plex.tv'] })).toThrow();
});
