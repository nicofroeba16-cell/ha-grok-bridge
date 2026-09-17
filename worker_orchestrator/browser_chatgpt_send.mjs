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

async function visibleHistorySearchInput(page) {
  const handle = await page.evaluateHandle(() => {
    const visible = (el) => {
      if (!el) return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const inputs = [...document.querySelectorAll('input')].filter(visible);
    for (const input of inputs) {
      const metadata = [
        input.getAttribute('placeholder'),
        input.getAttribute('aria-label'),
        input.getAttribute('name'),
        input.getAttribute('data-testid'),
      ].filter(Boolean).join(' ').toLowerCase();
      const searchish = /(search|suchen|chat|conversation|unterhaltung)/i.test(metadata);
      const overlay = input.closest('[role="dialog"], [role="menu"], [role="listbox"], [data-radix-popper-content-wrapper]');
      if (searchish || overlay) return input;
    }
    return null;
  });
  const element = handle.asElement();
  if (!element) await handle.dispose();
  return element;
}

async function waitForHistorySearchInput(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const input = await visibleHistorySearchInput(page);
    if (input) return input;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return null;
}

async function clickSidebarSearchTrigger(page) {
  const handle = await page.evaluateHandle(() => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => {
      if (!el) return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const candidates = [...document.querySelectorAll('button, a, [role="button"]')].filter(visible);
    const classified = candidates.map((el) => {
      const label = normalize([
        el.getAttribute('aria-label'),
        el.getAttribute('title'),
        el.getAttribute('data-testid'),
        el.innerText,
        el.textContent,
      ].filter(Boolean).join(' ')).toLowerCase();
      const sidebar = !!el.closest('nav, aside, [data-testid*="sidebar" i], [class*="sidebar" i]');
      const historySearch = (
        label === 'search' || label === 'suchen' ||
        label.includes('search chats') || label.includes('search chat') ||
        label.includes('chat search') || label.includes('chats durchsuchen') ||
        label.includes('unterhaltungen durchsuchen') ||
        label.includes('search-button') || label.includes('history-search')
      );
      const webSearch = label.includes('search the web') || label.includes('web search') || label.includes('web durchsuchen');
      return { el, sidebar, historySearch, webSearch };
    }).filter((item) => item.historySearch && !item.webSearch);
    const sidebarMatches = classified.filter((item) => item.sidebar);
    const selected = sidebarMatches.length === 1 ? sidebarMatches[0] : (classified.length === 1 ? classified[0] : null);
    return selected ? selected.el : null;
  });
  const element = handle.asElement();
  if (!element) {
    await handle.dispose();
    return false;
  }
  await element.click();
  return true;
}

async function openHistorySearch(page) {
  await page.keyboard.down('Control');
  await page.keyboard.press('KeyK');
  await page.keyboard.up('Control');

  let input = await waitForHistorySearchInput(page, 2500);
  if (input) return input;

  const clicked = await clickSidebarSearchTrigger(page);
  if (!clicked) {
    throw new Error('ChatGPT history search did not open via Ctrl+K and no unique sidebar Search/Suchen trigger was found');
  }
  input = await waitForHistorySearchInput(page, 8000);
  if (!input) throw new Error('ChatGPT history search input not found after sidebar search trigger');
  return input;
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

  const input = await openHistorySearch(page);
  await input.click();
  await page.keyboard.down('Control');
  await page.keyboard.press('KeyA');
  await page.keyboard.up('Control');
  await page.keyboard.type(title);
  await new Promise((resolve) => setTimeout(resolve, 1800));

  const matches = await page.evaluate((wanted) => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const found = [];
    for (const anchor of document.querySelectorAll('a[href*="/c/"]')) {
      const text = normalize(anchor.innerText || anchor.textContent || '');
      const firstLine = normalize((anchor.innerText || anchor.textContent || '').split('\n')[0]);
      if (text === wanted || firstLine === wanted || text.startsWith(`${wanted} `)) {
        found.push({ text, href: anchor.href });
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
  let ownsBrowser = false;
  let sendCommitted = false;
  try {
    const browserUrlRaw = String(process.env.CHATGPT_BROWSER_URL || '').trim();
    if (browserUrlRaw) {
      const browserUrl = new URL(browserUrlRaw);
      if (browserUrl.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(browserUrl.hostname) || !browserUrl.port || browserUrl.username || browserUrl.password) {
        throw new Error('CHATGPT_BROWSER_URL must be a credential-free loopback HTTP URL with an explicit port');
      }
      browser = await puppeteer.connect({ browserURL: browserUrl.toString() });
    } else {
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
      ownsBrowser = true;
    }

    const pages = await browser.pages();
    const page = pages.find((candidate) => candidate.url().startsWith('https://chatgpt.com')) || pages[0] || await browser.newPage();
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
    await page.keyboard.type(request.payload);

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

    const expectedPayload = normalizeText(request.payload);
    const matchingUserTurnsBefore = await page.evaluate((expected) => {
      const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      return [...document.querySelectorAll('[data-message-author-role="user"]')]
        .filter((turn) => normalize(turn.innerText || turn.textContent || '').includes(expected)).length;
    }, expectedPayload);

    // From this point onward a process interruption is delivery-uncertain.
    // The Python ledger deliberately never retries uncertain sends automatically.
    sendCommitted = true;
    await send.click();

    try {
      await page.waitForFunction(({ expected, before }) => {
        const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const matches = [...document.querySelectorAll('[data-message-author-role="user"]')]
          .filter((turn) => normalize(turn.innerText || turn.textContent || '').includes(expected)).length;
        return matches > before;
      }, { timeout: 12000, polling: 200 }, { expected: expectedPayload, before: matchingUserTurnsBefore });
    } catch (_) {
      throw new Error('post-send delivery could not be verified in ChatGPT conversation');
    }

    // A newly-rendered user turn can be optimistic UI only. Require persistence
    // across a full reload before declaring delivery successful.
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForFunction((expected) => {
        const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        return [...document.querySelectorAll('[data-message-author-role="user"]')]
          .some((turn) => normalize(turn.innerText || turn.textContent || '').includes(expected));
      }, { timeout: 15000, polling: 250 }, expectedPayload);
    } catch (_) {
      throw new Error('post-send delivery was not persisted after ChatGPT reload');
    }

    process.stdout.write(JSON.stringify({
      status: 'sent',
      message_id: request.messageId,
      delivery_verified: true,
      persisted_after_reload: true,
      output_scraped: false,
    }) + '\n');
  } catch (error) {
    fail(error, !sendCommitted);
  } finally {
    if (browser) {
      try {
        if (ownsBrowser) await browser.close();
        else await browser.disconnect();
      } catch (_) {}
    }
  }
}

await main();
