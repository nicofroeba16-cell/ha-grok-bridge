#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import puppeteer from 'puppeteer';

function fail(error, safeToRetry, code = 2) {
  process.stdout.write(JSON.stringify({ status: 'failed', error: String(error).slice(0, 240), safe_to_retry: !!safeToRetry }) + '\n');
  process.exit(code);
}

function normalizeText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function validateConcreteUrl(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.hostname !== 'chatgpt.com' || url.port || url.username || url.password) {
    throw new Error('destination must be a credential-free https://chatgpt.com URL');
  }
  const pathname = url.pathname.replace(/\/$/, '');
  if (!/^\/(?:g\/g-p-[A-Za-z0-9_-]+\/)?c\/[A-Za-z0-9-]+$/.test(pathname)) {
    throw new Error('destination must identify one concrete ChatGPT conversation');
  }
  url.search = '';
  url.hash = '';
  return url.toString();
}

function parseDestination(raw) {
  const value = String(raw || '').trim();
  if (value.startsWith('chat-title:')) {
    const title = normalizeText(value.slice('chat-title:'.length));
    if (!title || title.length > 200) throw new Error('chat-title destination is invalid');
    return { kind: 'title', title };
  }
  return { kind: 'url', url: validateConcreteUrl(value) };
}

async function readRequest() {
  const raw = fs.readFileSync(0, 'utf8').trim();
  if (!raw) throw new Error('empty request');
  const value = JSON.parse(raw);
  const messageId = String(value.message_id || '').trim();
  const destination = parseDestination(value.destination);
  const payload = String(value.payload || '');
  if (!messageId || !payload.trim()) throw new Error('message_id and payload are required');
  if (payload.length > 20000) throw new Error('payload exceeds browser wake size limit');
  return { messageId, destination, payload };
}

function loadCache() {
  const file = process.env.CHATGPT_ROUTE_CACHE_FILE || '';
  if (!file || !fs.existsSync(file)) return {};
  try {
    const value = JSON.parse(fs.readFileSync(file, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (_) {
    return {};
  }
}

function saveCache(cache) {
  const file = process.env.CHATGPT_ROUTE_CACHE_FILE || '';
  if (!file) return;
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(cache, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(tmp, file);
}

async function resolveByTitle(page, title) {
  const cache = loadCache();
  if (cache[title]) {
    try {
      return validateConcreteUrl(cache[title]);
    } catch (_) {
      delete cache[title];
      saveCache(cache);
    }
  }

  await page.goto('https://chatgpt.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await new Promise((resolve) => setTimeout(resolve, 1500));
  const initial = new URL(page.url());
  if (initial.hostname !== 'chatgpt.com') throw new Error('ChatGPT session redirected away from chatgpt.com');

  await page.keyboard.down('Control');
  await page.keyboard.press('KeyK');
  await page.keyboard.up('Control');

  await page.waitForFunction(() => {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')];
    return dialogs.some((dialog) => {
      const style = window.getComputedStyle(dialog);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      const input = dialog.querySelector('input');
      return !!input;
    });
  }, { timeout: 10000 });

  const inputHandle = await page.evaluateHandle(() => {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')];
    for (const dialog of dialogs) {
      const style = window.getComputedStyle(dialog);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const input = dialog.querySelector('input');
      if (input) return input;
    }
    return null;
  });
  const input = inputHandle.asElement();
  if (!input) throw new Error('ChatGPT history search input not found');
  await input.click();
  await page.keyboard.down('Control');
  await page.keyboard.press('KeyA');
  await page.keyboard.up('Control');
  await page.keyboard.insertText(title);
  await new Promise((resolve) => setTimeout(resolve, 1800));

  const matches = await page.evaluate((wanted) => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter((dialog) => {
      const style = window.getComputedStyle(dialog);
      return style.display !== 'none' && style.visibility !== 'hidden';
    });
    const roots = dialogs.length ? dialogs : [document];
    const found = [];
    for (const root of roots) {
      for (const anchor of root.querySelectorAll('a[href*="/c/"]')) {
        const text = normalize(anchor.innerText || anchor.textContent || '');
        const firstLine = normalize((anchor.innerText || anchor.textContent || '').split('\n')[0]);
        if (text === wanted || firstLine === wanted || text.startsWith(`${wanted} `)) {
          found.push({ text, href: anchor.href });
        }
      }
    }
    return found;
  }, title);

  const unique = [...new Map(matches.map((item) => [item.href, item])).values()];
  if (unique.length !== 1) {
    throw new Error(`exact chat title lookup returned ${unique.length} matches for ${title}`);
  }
  const resolved = validateConcreteUrl(unique[0].href);
  cache[title] = resolved;
  saveCache(cache);
  return resolved;
}

async function main() {
  let request;
  try {
    request = await readRequest();
  } catch (error) {
    fail(error, true);
  }

  const profileDir = process.env.CHATGPT_PROFILE_DIR || '';
  const chromeBin = process.env.CHATGPT_CHROME_BIN || '/usr/bin/google-chrome';
  if (!profileDir) fail('CHATGPT_PROFILE_DIR is required', true);
  if (!fs.existsSync(profileDir)) fail('CHATGPT_PROFILE_DIR does not exist', true);
  if (!fs.existsSync(chromeBin)) fail('CHATGPT_CHROME_BIN does not exist', true);

  let browser;
  let sendCommitted = false;
  try {
    browser = await puppeteer.launch({
      executablePath: chromeBin,
      userDataDir: profileDir,
      headless: process.env.CHATGPT_BROWSER_HEADLESS !== 'false',
      args: [
        '--no-first-run',
        '--disable-sync',
        '--disable-background-networking',
        '--disable-default-apps',
      ],
    });

    const pages = await browser.pages();
    const page = pages[0] || await browser.newPage();
    const destination = request.destination.kind === 'title'
      ? await resolveByTitle(page, request.destination.title)
      : request.destination.url;

    await page.goto(destination, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const loaded = new URL(page.url());
    if (loaded.hostname !== 'chatgpt.com') throw new Error('ChatGPT session redirected away from chatgpt.com');
    if (!loaded.pathname.includes('/c/')) throw new Error('ChatGPT conversation did not load; login may be required');

    const composerSelector = [
      '[data-testid="prompt-textarea"]',
      '#prompt-textarea',
      'textarea[data-testid="prompt-textarea"]',
    ].join(',');
    await page.waitForSelector(composerSelector, { visible: true, timeout: 20000 });
    const composer = await page.$(composerSelector);
    if (!composer) throw new Error('ChatGPT composer not found');
    await composer.focus();
    await page.keyboard.insertText(request.payload);

    const sendSelector = [
      'button[data-testid="send-button"]',
      'button[aria-label="Send prompt"]',
      'button[aria-label="Senden"]',
    ].join(',');
    await page.waitForSelector(sendSelector, { visible: true, timeout: 10000 });
    const send = await page.$(sendSelector);
    if (!send) throw new Error('ChatGPT send button not found');
    const disabled = await send.evaluate((el) => el.disabled || el.getAttribute('aria-disabled') === 'true');
    if (disabled) throw new Error('ChatGPT send button is disabled');

    // From this point onward a process interruption is delivery-uncertain.
    // The Python ledger deliberately never retries uncertain sends automatically.
    sendCommitted = true;
    await send.click();
    await new Promise((resolve) => setTimeout(resolve, 750));

    process.stdout.write(JSON.stringify({
      status: 'sent',
      message_id: request.messageId,
      output_scraped: false,
    }) + '\n');
  } catch (error) {
    fail(error, !sendCommitted);
  } finally {
    if (browser) {
      try { await browser.close(); } catch (_) {}
    }
  }
}

await main();
