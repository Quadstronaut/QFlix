import { defineConfig } from '@playwright/test';

// Boots the real adapter-node production server (build must exist) and runs the
// e2e checks against it. SESSION_SECRET is a throwaway for the test run.
export default defineConfig({
	testDir: 'tests/e2e',
	timeout: 30_000,
	webServer: {
		command: 'node build/index.js',
		port: 3000,
		env: { PORT: '3000', HOST: '127.0.0.1', SESSION_SECRET: 'e2e-secret-not-real' },
		reuseExistingServer: false,
		stdout: 'ignore',
		stderr: 'pipe'
	},
	use: { baseURL: 'http://localhost:3000' }
});
