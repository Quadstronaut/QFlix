import { expect, test, beforeAll } from 'vitest';

beforeAll(() => {
	process.env.SESSION_SECRET = 'test-secret-please-change';
});

test('auth cookie round-trips and rejects tamper/garbage/undefined', async () => {
	const { signAuth, verifyAuth } = await import('./session');
	const tok = signAuth({ u: 'kyle', e: 'k@x.com' });
	expect(verifyAuth(tok)).toMatchObject({ u: 'kyle', e: 'k@x.com' });
	expect(verifyAuth(tok.slice(0, -2) + 'xx')).toBeNull();
	expect(verifyAuth(undefined)).toBeNull();
	expect(verifyAuth('garbage')).toBeNull();
});

test('expired auth token is rejected', async () => {
	const { signAuth, verifyAuth } = await import('./session');
	const tok = signAuth({ u: 'a', e: 'a@x' }, -10);
	expect(verifyAuth(tok)).toBeNull();
});

test('greeting cookie round-trips', async () => {
	const { signGreeting, verifyGreeting } = await import('./session');
	expect(verifyGreeting(signGreeting('Kyle'))).toEqual({ n: 'Kyle' });
});

test('pin cookie round-trips', async () => {
	const { signPin, verifyPin } = await import('./session');
	expect(verifyPin(signPin(77))).toEqual({ pin: 77 });
});
