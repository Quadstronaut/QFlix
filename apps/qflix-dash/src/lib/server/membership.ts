// Membership gate: Plex shared-users (primary) -> Seerr (fallback). Fail-closed:
// only an explicit match grants; any source error just means that source misses.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { cfg } from './env';

const pexec = promisify(execFile);

export interface Account {
	id: number;
	email: string;
	username: string;
}

/** Pure: does `who` appear in the Plex authorized-accounts set? */
export function matchPlex(members: Account[], who: { id: number; email: string }): boolean {
	const email = who.email.toLowerCase();
	return members.some((m) => m.id === who.id || (!!m.email && m.email.toLowerCase() === email));
}

/** Pure: does `who` appear in Seerr's user list? */
export function matchSeerr(users: Array<{ plexId?: number; email?: string }>, who: { id: number; email: string }): boolean {
	const email = who.email.toLowerCase();
	return users.some(
		(u) => Number(u.plexId) === who.id || (!!u.email && String(u.email).toLowerCase() === email)
	);
}

let memberCache: { t: number; set: Account[] } | null = null;
const TTL = 600_000; // 10 min

async function plexMembers(): Promise<Account[]> {
	if (memberCache && Date.now() - memberCache.t < TTL) return memberCache.set;
	const c = cfg();
	if (!c.plexMembersPy) return [];
	const [bin, ...args] = c.plexMembersPy.split(' ').filter(Boolean);
	const { stdout } = await pexec(bin, args, {
		timeout: 15000,
		env: { ...process.env, PLEX_TOKEN: c.plexToken }
	});
	const set = JSON.parse(stdout) as Account[];
	memberCache = { t: Date.now(), set };
	return set;
}

async function seerrUsers(): Promise<Array<{ plexId?: number; email?: string }>> {
	const c = cfg();
	if (!c.seerrUrl || !c.seerrKey) return [];
	const r = await fetch(`${c.seerrUrl}/api/v1/user?take=200`, { headers: { 'X-Api-Key': c.seerrKey } });
	if (!r.ok) return [];
	const j = await r.json();
	return j.results ?? j ?? [];
}

export async function isMember(who: { id: number; email: string }): Promise<'plex' | 'seerr' | null> {
	try {
		if (matchPlex(await plexMembers(), who)) return 'plex';
	} catch {
		/* fall through to seerr */
	}
	try {
		if (matchSeerr(await seerrUsers(), who)) return 'seerr';
	} catch {
		/* fail-closed */
	}
	return null;
}
