import { json } from '@sveltejs/kit';
import { getStatus } from '#lib/server/status.js';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async () => json(await getStatus());
