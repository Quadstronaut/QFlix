import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import type { Usage } from '$lib/usage';
import { cfg } from './env';

const pexec = promisify(execFile);

// The script needs a ~1s sample window to compute CPU deltas, so each run costs
// ~1s. Cache briefly and dedupe concurrent misses so N viewers share one run.
let cache: { t: number; val: Usage } | null = null;
let inflight: Promise<Usage> | null = null;
const TTL = 4000;

async function run(): Promise<Usage> {
	const c = cfg();
	if (!c.qflixTopBin) throw new Error('QFLIX_TOP_BIN unset');
	const { stdout } = await pexec(c.qflixTopBin, ['--json', '--interval', '1'], {
		timeout: 8000,
		maxBuffer: 2 * 1024 * 1024
	});
	return JSON.parse(stdout) as Usage;
}

export async function getUsage(): Promise<Usage> {
	if (cache && Date.now() - cache.t < TTL) return cache.val;
	if (inflight) return inflight; // a run is already in progress — ride it
	inflight = run()
		.then((val) => {
			cache = { t: Date.now(), val };
			return val;
		})
		.finally(() => {
			inflight = null;
		});
	return inflight;
}
