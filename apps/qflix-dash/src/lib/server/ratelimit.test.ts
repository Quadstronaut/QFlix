import { expect, test } from 'vitest';
import { allow } from './ratelimit';

test('allows up to max within window, then blocks', () => {
	const k = 'unit-key';
	expect(allow(k, 2, 10_000)).toBe(true);
	expect(allow(k, 2, 10_000)).toBe(true);
	expect(allow(k, 2, 10_000)).toBe(false);
});
