// HMAC-signed, stateless cookies. No server-side store.
//  - AUTH (qd_s): short-lived (30 min), gates Support submit; carries {u,e}.
//  - GREET (qd_g): long-lived (30 d), cosmetic display name only; never authorizes.
//  - qd_pin: short-lived PIN handle during the Plex sign-in round-trip.
import { createHmac, timingSafeEqual } from 'node:crypto';

export const AUTH = 'qd_s';
export const GREET = 'qd_g';
export const PIN = 'qd_pin';

function secret(): string {
	const s = process.env.SESSION_SECRET;
	if (!s) throw new Error('SESSION_SECRET unset');
	return s;
}

function sign(payload: object, ttlSec: number): string {
	const body = { ...payload, exp: Math.floor(Date.now() / 1000) + ttlSec };
	const p = Buffer.from(JSON.stringify(body)).toString('base64url');
	const sig = createHmac('sha256', secret()).update(p).digest('base64url');
	return `${p}.${sig}`;
}

function verify<T extends object>(token: string | undefined): (T & { exp: number }) | null {
	if (!token || !token.includes('.')) return null;
	const [p, sig] = token.split('.');
	const expected = createHmac('sha256', secret()).update(p).digest('base64url');
	const a = Buffer.from(sig);
	const b = Buffer.from(expected);
	if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
	try {
		const body = JSON.parse(Buffer.from(p, 'base64url').toString('utf8'));
		if (typeof body.exp !== 'number' || body.exp < Math.floor(Date.now() / 1000)) return null;
		return body;
	} catch {
		return null;
	}
}

export function signAuth(p: { u: string; e: string }, ttlSec = 1800): string {
	return sign(p, ttlSec);
}
export function verifyAuth(token: string | undefined): { u: string; e: string } | null {
	const b = verify<{ u: string; e: string }>(token);
	return b ? { u: b.u, e: b.e } : null;
}

export function signGreeting(name: string, ttlSec = 2592000): string {
	return sign({ n: name }, ttlSec);
}
export function verifyGreeting(token: string | undefined): { n: string } | null {
	const b = verify<{ n: string }>(token);
	return b ? { n: b.n } : null;
}

export function signPin(id: number, ttlSec = 600): string {
	return sign({ pin: id }, ttlSec);
}
export function verifyPin(token: string | undefined): { pin: number } | null {
	const b = verify<{ pin: number }>(token);
	return b ? { pin: b.pin } : null;
}
