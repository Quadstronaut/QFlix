import { test, expect, devices } from '@playwright/test';

// The whole point of the rebuild: a mobile board that doesn't clip labels.
test.use({ ...devices['Pixel 5'] });

test('board renders 6 tiles with the marker and no clipped labels', async ({ page }) => {
	await page.goto('/');
	await expect(page.locator('body[data-qflix-dash]')).toBeAttached();

	const labels = page.locator('.lbl');
	await expect(labels.first()).toBeVisible();
	expect(await labels.count()).toBe(6);

	const n = await labels.count();
	for (let i = 0; i < n; i++) {
		const el = labels.nth(i);
		const clipped = await el.evaluate((e: HTMLElement) => e.scrollWidth > e.clientWidth + 1);
		expect(clipped, `label clipped: ${await el.innerText()}`).toBe(false);
	}
});

test('healthz responds ok', async ({ request }) => {
	const r = await request.get('/healthz');
	expect(r.status()).toBe(200);
	expect(await r.text()).toBe('ok');
});
