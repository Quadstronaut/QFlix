export const prerender = false;

export const GET = () =>
	new Response('ok', { status: 200, headers: { 'content-type': 'text/plain' } });
