// In-memory sliding-window limiter. Single-process (adapter-node) — adequate for
// a low-volume support form; resets on restart.
const hits = new Map<string, number[]>();

export function allow(key: string, max = 3, windowMs = 3_600_000): boolean {
	const now = Date.now();
	const arr = (hits.get(key) ?? []).filter((t) => now - t < windowMs);
	if (arr.length >= max) {
		hits.set(key, arr);
		return false;
	}
	arr.push(now);
	hits.set(key, arr);
	return true;
}
