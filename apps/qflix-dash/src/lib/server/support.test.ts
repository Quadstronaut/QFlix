import { expect, test } from 'vitest';
import { validateMessage, buildWebhookPayload } from './support';

test('validateMessage trims and bounds 1..2000', () => {
	expect(validateMessage('  hi  ')).toBe('hi');
	expect(validateMessage('')).toBeNull();
	expect(validateMessage('   ')).toBeNull();
	expect(validateMessage('x'.repeat(2001))).toBeNull();
	expect(validateMessage(123)).toBeNull();
});

test('payload uses session identity + Q avatar, never client input', () => {
	const p = buildWebhookPayload({ u: 'kyle', e: 'k@x.com' }, 'help me', 'https://q/Q.png', '2026-06-27T00:00:00Z');
	expect(p.username).toBe('QFlix');
	expect(p.avatar_url).toBe('https://q/Q.png');
	expect(p.embeds[0].fields[0].value).toBe('kyle (k@x.com)');
	expect(p.embeds[0].description).toBe('help me');
	expect(p.embeds[0].timestamp).toBe('2026-06-27T00:00:00Z');
});
