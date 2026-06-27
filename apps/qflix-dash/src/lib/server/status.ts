import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import type { TileState } from '$lib/tiles';
import { cfg } from './env';

const pexec = promisify(execFile);

interface MaintApp {
	app: string;
	ok: boolean;
}
export interface MaintJson {
	summary?: { total: number; up: number; down: number };
	apps: MaintApp[];
}

const CORE = new Set(['plex', 'seerr']);

/** Pure: map the manitoba-maint status JSON to tile pucks. */
export function mapMaint(j: MaintJson): Record<string, TileState> {
	const byApp = new Map(j.apps.map((a) => [a.app, a.ok]));
	const state = (k: string): TileState =>
		byApp.get(k) ? 'ok' : byApp.has(k) ? 'down' : 'unknown';
	const coreDown = j.apps.some((a) => CORE.has(a.app) && !a.ok);
	const status: TileState = coreDown ? 'down' : (j.summary?.down ?? 0) > 0 ? 'warn' : 'ok';
	return { plex: state('plex'), seerr: state('seerr'), status };
}

async function reach(url: string, ms = 3000): Promise<TileState> {
	if (!url) return 'unknown';
	try {
		const ac = new AbortController();
		const t = setTimeout(() => ac.abort(), ms);
		const r = await fetch(url, { method: 'HEAD', signal: ac.signal });
		clearTimeout(t);
		// 405/403 still proves the host is reachable
		return r.ok || r.status === 405 || r.status === 403 ? 'ok' : 'down';
	} catch {
		return 'down';
	}
}

let cache: { t: number; val: Record<string, TileState> } | null = null;
const TTL = 30_000;

export async function getStatus(): Promise<Record<string, TileState>> {
	if (cache && Date.now() - cache.t < TTL) return cache.val;
	const c = cfg();
	let maint: Record<string, TileState> = { plex: 'unknown', seerr: 'unknown', status: 'unknown' };
	try {
		const { stdout } = await pexec(c.maintBin, ['status', '--all', '--json'], {
			timeout: 8000,
			maxBuffer: 4 * 1024 * 1024
		});
		maint = mapMaint(JSON.parse(stdout));
	} catch {
		/* leave unknown — never block the board */
	}
	const [github, faq] = await Promise.all([reach('https://api.github.com/', 3000), reach(c.faqUrl, 3000)]);
	const val: Record<string, TileState> = { ...maint, github, faq, support: 'ok' };
	cache = { t: Date.now(), val };
	return val;
}
