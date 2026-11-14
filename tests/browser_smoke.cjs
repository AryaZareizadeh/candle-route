const assert = require('node:assert/strict');
const {spawn, execFileSync} = require('node:child_process');
const {mkdtempSync} = require('node:fs');
const {once} = require('node:events');
const {tmpdir} = require('node:os');
const {join} = require('node:path');
const {setTimeout: delay} = require('node:timers/promises');
const {chromium} = require('playwright');

const password = 'candle-route-browser-smoke';
const base = 'http://127.0.0.1:18011';

async function smoke() {
    const root = mkdtempSync(join(tmpdir(), 'candle-route-browser-'));
    const python = process.env.CANDLEROUTE_TEST_PYTHON || 'python';
    const env = {...process.env, TRADER_DATA_DIR: root, TRADER_ENV_FILE: join(root, '.env'),
        TRADER_MULTIUSER: 'false', TRADER_OFFLINE: 'true', HF_HUB_OFFLINE: '1',
        DASHBOARD_ALLOWED_HOSTS: '127.0.0.1', DASHBOARD_SECURE_COOKIE: 'false'};
    for (const name of Object.keys(env)) if (name.startsWith('NOBITEX_')) delete env[name];
    let server;
    let browser;
    try {
        env.DASHBOARD_PASSWORD_HASH = execFileSync(python, ['-c',
            'import sys; from auth import hash_password; print(hash_password(sys.argv[1]))', password],
        {env, encoding: 'utf8'}).trim();
        server = spawn(python, ['main.py', '--port', '18011'], {env, stdio: 'inherit'});
        let ready;
        for (let attempt = 0; attempt < 100; attempt++) {
            const health = await fetch(`${base}/healthz`).catch(() => null);
            if (health?.ok) { ready = true; break; }
            if (server.exitCode !== null) break;
            await delay(150);
        }
        assert.ok(ready, 'CandleRoute did not start');
        browser = await chromium.launch({channel: process.env.CANDLEROUTE_TEST_BROWSER});
        const page = await browser.newPage();
        await page.goto(base);
        await page.locator('#loginForm input[type=password]').fill(password);
        await page.getByRole('button', {name: 'Sign in', exact: true}).click();
        const response = page.waitForResponse(res => new URL(res.url()).pathname === '/forecasting/api/bootstrap');
        await page.getByRole('button', {name: 'Forecasting', exact: true}).click();
        assert.equal((await response).status(), 200);
        const frame = page.frameLocator('#forecastingFrame');
        await frame.getByRole('heading', {name: 'Chart forecasts'}).waitFor();
        await frame.locator('#connection-text').getByText('Connected', {exact: true}).waitFor();
        const chart = await page.locator('#forecastingFrame').evaluate(async iframe => {
            try {
                await iframe.contentWindow.ForecastConnection.api('/api/chart/preview', {
                    method: 'POST', body: JSON.stringify({unknown_setting: true}),
                });
            } catch (error) {
                return error.message;
            }
            return null;
        });
        assert.match(chart, /unknown chart forecast setting/i);
        assert.equal(await page.locator('#forecastingFrame[src^="/forecasting/"]').count(), 1);
        await page.getByRole('button', {name: /sign out|log out/i}).click();
        await page.locator('#loginForm input[type=password]').waitFor({state: 'visible'});
        assert.equal(await page.locator('iframe[src^="/forecasting/"]').count(), 0);
        assert.equal((await page.request.get(`${base}/forecasting/api/bootstrap`)).status(), 401);
        console.log('Trading login, embedded forecasts and logout passed');
    } finally {
        await browser?.close();
        if (server && server.exitCode === null) {
            const exited = once(server, 'exit');
            server.kill();
            await exited;
        }
        execFileSync(python, ['-c', 'import shutil, sys; shutil.rmtree(sys.argv[1])', root], {env});
    }
}

smoke().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
