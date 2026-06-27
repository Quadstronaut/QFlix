export type TileState = 'ok' | 'warn' | 'down' | 'unknown';

export interface TileDef {
	key: string;
	label: string;
	icon: string;
	/** Link target. Root-relative paths resolve against the live host; http(s) opens in a new tab. */
	href?: string;
	/** When set, the tile is a button that triggers an in-page action instead of navigating. */
	action?: 'support';
	/** Invert the icon (for dark monochrome glyphs like GitHub) so it shows on navy. */
	invert?: boolean;
	/** Key into the /api/status payload that drives this tile's status puck. */
	statusKey: string;
}

// Audiobooks + Comics intentionally dropped for v1 (spec §17). Admin tiles stay on the SSH tunnel.
export const TILES: TileDef[] = [
	{ key: 'seerr', label: 'Requests', icon: '/icons/seerr.svg', href: '/seerr/', statusKey: 'seerr' },
	{ key: 'plex', label: 'Watch', icon: '/icons/plex.svg', href: '/web/', statusKey: 'plex' },
	{ key: 'status', label: 'Status', icon: '/icons/kuma.svg', href: '/status/manitoba', statusKey: 'status' },
	{
		key: 'github',
		label: 'Source',
		icon: '/icons/github.svg',
		href: 'https://github.com/Quadstronaut/QFlix',
		invert: true,
		statusKey: 'github'
	},
	{ key: 'faq', label: 'FAQ', icon: '/icons/faq.svg', href: '/faq/', statusKey: 'faq' },
	{ key: 'support', label: 'Support', icon: '/icons/support.svg', action: 'support', statusKey: 'support' }
];
