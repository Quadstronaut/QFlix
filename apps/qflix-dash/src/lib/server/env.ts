// Server-only config. `$lib/server/*` is never bundled to the client, so reading
// process.env here is safe (and trivially testable in node-env vitest). adapter-node
// passes the systemd EnvironmentFile through to process.env at runtime.
export interface Cfg {
	maintBin: string;
	plexToken: string;
	plexClientId: string;
	plexMembersPy: string; // "<python> <script>"
	seerrUrl: string; // e.g. http://127.0.0.1:42011
	seerrKey: string;
	discordWebhook: string;
	sessionSecret: string;
	qAvatar: string;
	faqUrl: string;
	qflixTopBin: string; // path to scripts/qflix-top-pub.sh (powers /api/usage)
}

export function cfg(): Cfg {
	const e = process.env;
	return {
		maintBin: e.MANITOBA_MAINT_BIN || 'manitoba-maint',
		plexToken: e.PLEX_TOKEN || '',
		plexClientId: e.PLEX_CLIENT_ID || 'qflix-dashboard',
		plexMembersPy: e.PLEX_MEMBERS_PY || '',
		seerrUrl: e.SEERR_URL || '',
		seerrKey: e.SEERR_API_KEY || '',
		discordWebhook: e.DISCORD_WEBHOOK || '',
		sessionSecret: e.SESSION_SECRET || '',
		qAvatar: e.Q_AVATAR_URL || 'https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png',
		faqUrl: e.FAQ_PROBE_URL || '',
		qflixTopBin: e.QFLIX_TOP_BIN || ''
	};
}
