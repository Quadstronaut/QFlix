import { expect, test } from 'vitest';
import { mapMaint } from './status';

test('core app down => status down; per-app states correct', () => {
	const s = mapMaint({
		summary: { total: 3, up: 2, down: 1 },
		apps: [
			{ app: 'plex', ok: true },
			{ app: 'seerr', ok: false },
			{ app: 'sonarr', ok: true }
		]
	});
	expect(s.plex).toBe('ok');
	expect(s.seerr).toBe('down');
	expect(s.status).toBe('down');
});

test('non-core down => warn; all up => ok; missing => unknown', () => {
	const warn = mapMaint({
		summary: { total: 3, up: 2, down: 1 },
		apps: [
			{ app: 'plex', ok: true },
			{ app: 'seerr', ok: true },
			{ app: 'sonarr', ok: false }
		]
	});
	expect(warn.status).toBe('warn');

	const ok = mapMaint({
		summary: { total: 2, up: 2, down: 0 },
		apps: [
			{ app: 'plex', ok: true },
			{ app: 'seerr', ok: true }
		]
	});
	expect(ok.status).toBe('ok');

	const missing = mapMaint({ summary: { total: 0, up: 0, down: 0 }, apps: [] });
	expect(missing.plex).toBe('unknown');
});
