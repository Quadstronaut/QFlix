import { expect, test } from 'vitest';
import { matchPlex, matchSeerr } from './membership';

test('matchPlex matches by id or email (case-insensitive)', () => {
	const members = [{ id: 1, email: 'a@x.com', username: 'a' }];
	expect(matchPlex(members, { id: 1, email: 'other@x.com' })).toBe(true); // id hit
	expect(matchPlex(members, { id: 9, email: 'A@X.com' })).toBe(true); // email hit, case-insensitive
	expect(matchPlex(members, { id: 9, email: 'c@x.com' })).toBe(false);
});

test('matchSeerr matches by plexId or email', () => {
	const users = [{ plexId: 9, email: 'b@x.com' }];
	expect(matchSeerr(users, { id: 9, email: 'no@x' })).toBe(true);
	expect(matchSeerr(users, { id: 1, email: 'B@x.com' })).toBe(true);
	expect(matchSeerr(users, { id: 1, email: 'c@x' })).toBe(false);
});
