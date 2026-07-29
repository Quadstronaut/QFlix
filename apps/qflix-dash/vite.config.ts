import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// adapter-auto only supports some environments, see https://svelte.dev/docs/kit/adapter-auto for a list.
			// If your environment is not supported, or you settled on a specific environment, switch out the adapter.
			// See https://svelte.dev/docs/kit/adapters for more information about adapters.
			adapter: adapter(),
			// The board's tiles link to OTHER apps on the same host (Plex /web/,
			// Seerr /seerr/, Kuma /status/manitoba, FAQ /faq/) — not routes of this
			// app. Don't let the prerender crawler follow them.
			prerender: { crawl: false }
		})
	],
	test: {
		// Two projects, because the suites need different module resolution.
		// The server suites run under node. Component suites need jsdom AND
		// `resolve.conditions: ['browser']` — without the browser condition Svelte
		// resolves to its SERVER build and mount() throws
		// `lifecycle_function_unavailable`, which a per-file @vitest-environment
		// docblock cannot fix since it sets the environment but not resolution.
		// Component tests are named *.svelte.test.ts so the split is mechanical.
		projects: [
			{
				extends: true,
				test: {
					name: 'server',
					environment: 'node',
					include: ['src/**/*.test.ts'],
					exclude: ['src/**/*.svelte.test.ts']
				}
			},
			{
				extends: true,
				resolve: { conditions: ['browser'] },
				test: {
					name: 'client',
					environment: 'jsdom',
					include: ['src/**/*.svelte.test.ts']
				}
			}
		]
	}
});
