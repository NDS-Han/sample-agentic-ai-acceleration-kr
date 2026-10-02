import { chromium } from 'playwright-core';
import fs from 'fs';
const line = fs.readFileSync('/tmp/cj.txt', 'utf8').split('\n').filter(l => l.includes('admin_jwt')).pop().trim().split('\t');
const [, , , , , name, value] = line;
const browser = await chromium.launch({
  executablePath: '/home/ubuntu/.cache/puppeteer/chrome/linux-154.0.8037.92/chrome-linux64/chrome',
  args: ['--no-sandbox'],
});
const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 }, locale: 'ko-KR' });
await ctx.addCookies([{ name, value, domain: 'localhost', path: '/' }]);
const page = await ctx.newPage();
await page.goto('http://localhost:3000/models', { waitUntil: 'networkidle' });
await page.getByRole('button', { name: /모델 추가/ }).click();
await page.waitForTimeout(600);
await page.screenshot({ path: '/tmp/addmodel-hint.png' });
await browser.close();
console.log('done');
