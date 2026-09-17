import process from 'node:process';
import puppeteer from 'puppeteer';

function browserEndpoint() {
  const raw = String(process.env.CHATGPT_BROWSER_URL || '').trim();
  if (!raw) throw new Error('CHATGPT_BROWSER_URL is required');
  const url = new URL(raw);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || !url.port || url.username || url.password) {
    throw new Error('CHATGPT_BROWSER_URL must be a credential-free loopback HTTP URL with an explicit port');
  }
  return url.toString();
}

function waitSeconds() {
  const arg = process.argv.find((value) => value.startsWith('--wait='));
  if (!arg) return 0;
  const value = Number(arg.slice('--wait='.length));
  if (!Number.isFinite(value) || value < 0 || value > 600) throw new Error('--wait must be between 0 and 600 seconds');
  return value;
}

function pathKind(raw) {
  try {
    const path = new URL(raw).pathname;
    if (/^\/g\/g-p-[^/]+\/c\//.test(path)) return 'project-conversation';
    if (/^\/c\//.test(path)) return 'conversation';
    if (path === '/' || path === '') return 'home';
    return 'other';
  } catch (_) {
    return 'other';
  }
}

async function probe() {
  let browser;
  try {
    browser = await puppeteer.connect({ browserURL: browserEndpoint() });
    const pages = await browser.pages();
    const page = pages.find((candidate) => candidate.url().startsWith('https://chatgpt.com'));
    if (!page) return { state: 'BROWSER_READY_NO_CHATGPT' };

    const title = await page.title();
    const challengeFrame = page.frames().some((frame) => frame.url().includes('challenges.cloudflare.com'));
    if (/just a moment|nur einen moment/i.test(title) || challengeFrame) {
      return { state: 'BLOCKED_CHALLENGE', path_kind: pathKind(page.url()) };
    }

    const ui = await page.evaluate(() => ({
      composer: !!document.querySelector('[data-testid="prompt-textarea"],#prompt-textarea,textarea[data-testid="prompt-textarea"]'),
      login: !!document.querySelector('a[href*="/auth/login"],button[data-testid*="login"],a[data-testid*="login"]'),
    }));
    if (ui.composer) return { state: 'HEALTHY', path_kind: pathKind(page.url()) };
    if (ui.login || page.url().includes('/auth/')) return { state: 'BLOCKED_AUTH', path_kind: pathKind(page.url()) };
    return { state: 'DEGRADED_UI', path_kind: pathKind(page.url()) };
  } catch (error) {
    return { state: 'BROWSER_DOWN', error: `${error.name || 'Error'}: ${error.message || error}`.slice(0, 180) };
  } finally {
    if (browser) {
      try { await browser.disconnect(); } catch (_) {}
    }
  }
}

const wait = waitSeconds();
const deadline = Date.now() + wait * 1000;
let result;
do {
  result = await probe();
  if (result.state === 'HEALTHY') break;
  if (Date.now() >= deadline) break;
  await new Promise((resolve) => setTimeout(resolve, 1000));
} while (true);

process.stdout.write(`${JSON.stringify(result)}\n`);
process.exit(result.state === 'HEALTHY' ? 0 : 2);
