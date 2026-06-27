// Pure helpers for the Support endpoint — identity always comes from the verified
// session, never from client input.
export function validateMessage(m: unknown): string | null {
	if (typeof m !== 'string') return null;
	const t = m.trim();
	return t.length >= 1 && t.length <= 2000 ? t : null;
}

export interface DiscordEmbed {
	title: string;
	description: string;
	fields: Array<{ name: string; value: string }>;
	color: number;
	timestamp?: string;
}
export interface DiscordPayload {
	username: string;
	avatar_url: string;
	embeds: DiscordEmbed[];
}

export function buildWebhookPayload(
	who: { u: string; e: string },
	message: string,
	avatar: string,
	timestamp?: string
): DiscordPayload {
	const embed: DiscordEmbed = {
		title: 'Support request',
		description: message,
		fields: [{ name: 'From', value: `${who.u} (${who.e})` }],
		color: 0xff8c42
	};
	if (timestamp) embed.timestamp = timestamp;
	return { username: 'QFlix', avatar_url: avatar, embeds: [embed] };
}
