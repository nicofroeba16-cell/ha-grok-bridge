#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createHash } from 'node:crypto';
import puppeteer from 'puppeteer';

function fail(error, safeToRetry, code = 2) {
  process.stdout.write(JSON.stringify({ status: 'failed', error: String(error).slice(0, 240), safe_to_retry: !!safeToRetry }) + '\n');
  process.exit(code);
}

function normalizeText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}


const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function canonicalComposerText(value) {
  return String(value || '').replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ');
}

async function readConversationBusyState(page) {
  return page.evaluate(() => {
    const visible = (el) => {
      if (!el) return false;
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    const stopSelectors = [
      'button[data-testid="stop-button"]',
      'button[aria-label="Stop generating"]',
      'button[aria-label="Generierung stoppen"]',
      'button[aria-label="Antwortgenerierung beenden"]',
    ];
    const stopControl = document.querySelector(stopSelectors.join(','));
    if (visible(stopControl)) {
      return { busy: true, reason: 'stop_control_visible' };
    }

    const busyCandidates = [...document.querySelectorAll(
      '[aria-busy="true"], [data-state="streaming"], [data-is-streaming="true"], button, [role="button"]'
    )].filter(visible);
    for (const element of busyCandidates) {
      const metadata = [
        element.getAttribute('aria-label'),
        element.getAttribute('title'),
        element.getAttribute('data-testid'),
        element.getAttribute('data-state'),
        element.textContent,
      ].filter(Boolean).join(' ').toLowerCase();
      if (
        metadata.includes('stop generating') ||
        metadata.includes('generierung stoppen') ||
        metadata.includes('antwortgenerierung beenden') ||
        metadata.includes('streaming') ||
        metadata.includes('generating')
      ) {
        return { busy: true, reason: 'positive_generation_indicator' };
      }
    }
    return { busy: false, reason: 'idle' };
  });
}

async function waitForStableConversationIdle(
  page,
  timeoutMs = Number(process.env.CHATGPT_IDLE_TIMEOUT_MS || 120000),
  stableMs = Number(process.env.CHATGPT_IDLE_STABLE_MS || 1500),
) {
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  let idleSince = 0;
  while (Date.now() < deadline) {
    let state = { busy: true, reason: 'probe_failed' };
    try {
      state = await readConversationBusyState(page);
    } catch (_) {
      state = { busy: true, reason: 'probe_failed' };
    }
    if (!state.busy) {
      if (!idleSince) idleSince = Date.now();
      if (Date.now() - idleSince >= Math.max(250, stableMs)) {
        return { idle: true, stable_for_ms: Date.now() - idleSince };
      }
    } else {
      idleSince = 0;
    }
    await sleep(250);
  }
  return { idle: false, stable_for_ms: 0 };
}

async function composerText(page, composer) {
  return page.evaluate((el) => {
    if (!el) return '';
    if ('value' in el && typeof el.value === 'string') return el.value;
    if (el.isContentEditable && el.children?.length) {
      // ProseMirror renders newline-delimited input as block paragraphs.
      // innerText inserts an extra visual blank line between <p> nodes, while
      // textContent removes delimiters entirely. Reconstruct one logical line
      // per direct block so atomic readback matches the original payload.
      const blocks = [...el.children];
      const blockTags = new Set(['P', 'DIV']);
      if (blocks.every((child) => blockTags.has(child.tagName))) {
        return blocks.map((child) => child.innerText ?? child.textContent ?? '').join('\n');
      }
    }
    return el.innerText ?? el.textContent ?? '';
  }, composer);
}

async function setComposerTextAtomically(page, composer, payload) {
  await page.evaluate((el, text) => {
    if (!el) throw new Error('composer missing during atomic insertion');
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'input') {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (!setter) throw new Error('native composer value setter unavailable');
      setter.call(el, text);
      el.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertText',
        data: text,
      }));
      return;
    }

    if (!el.isContentEditable) throw new Error('unsupported ChatGPT composer element');
    el.focus();
    const selection = window.getSelection();
    if (!selection) throw new Error('composer selection unavailable');
    const range = document.createRange();
    range.selectNodeContents(el);
    selection.removeAllRanges();
    selection.addRange(range);

    // One insertText operation is deliberately used for the complete payload.
    // Embedded newlines are data, not KeyboardEvent Enter presses.
    const inserted = document.execCommand('insertText', false, text);
    if (!inserted) {
      el.replaceChildren(document.createTextNode(text));
      el.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertText',
        data: text,
      }));
    }
  }, composer, payload);
}

async function countWakeTurns(page, fullPayload, firstLine) {
  return page.evaluate(({ fullPayload, firstLine }) => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const full = normalize(fullPayload);
    const prefix = normalize(firstLine);
    let exactFull = 0;
    let exactPrefixOnly = 0;
    for (const turn of document.querySelectorAll('[data-message-author-role="user"]')) {
      const content = turn.querySelector('[data-testid="collapsible-user-message-content"]')
        || turn.querySelector('.whitespace-pre-wrap') || turn;
      const text = normalize(content.innerText || content.textContent || '');
      if (text === full) exactFull += 1;
      if (text === prefix && text !== full) exactPrefixOnly += 1;
    }
    return { exactFull, exactPrefixOnly };
  }, { fullPayload, firstLine });
}

async function waitForExactlyOneWakeTurn(page, fullPayload, firstLine, before, timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const counts = await countWakeTurns(page, fullPayload, firstLine);
      if (
        counts.exactFull === before.exactFull + 1 &&
        counts.exactPrefixOnly === before.exactPrefixOnly
      ) return true;
      if (counts.exactFull > before.exactFull + 1 || counts.exactPrefixOnly > before.exactPrefixOnly) {
        return false;
      }
    } catch (_) {}
    await sleep(250);
  }
  return false;
}

async function waitForAssistantCompletion(page, before, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  let stableSince = 0;
  while (Date.now() < deadline) {
    let complete = false;
    try {
      complete = await page.evaluate((before) => {
        const assistantTurns = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
        const currentIds = assistantTurns
          .map((turn) => turn.getAttribute('data-message-id'))
          .filter(Boolean);
        const previousIds = new Set(before.ids || []);
        const hasNewIdentity = currentIds.some((id) => !previousIds.has(id));
        const identityAvailable = currentIds.length > 0 || previousIds.size > 0;
        const hasNewAssistant = identityAvailable ? hasNewIdentity : assistantTurns.length > before.count;
        const stop = document.querySelector([
          'button[data-testid="stop-button"]',
          'button[aria-label="Stop generating"]',
          'button[aria-label="Generierung stoppen"]',
          'button[aria-label="Antwortgenerierung beenden"]',
        ].join(','));
        const composer = document.querySelector([
          '[data-testid="prompt-textarea"]',
          '#prompt-textarea',
          'textarea[data-testid="prompt-textarea"]',
        ].join(','));
        return hasNewAssistant && !stop && !!composer;
      }, before);
    } catch (_) {
      complete = false;
    }
    if (complete) {
      if (!stableSince) stableSince = Date.now();
      if (Date.now() - stableSince >= 1500) return true;
    } else {
      stableSince = 0;
    }
    await sleep(300);
  }
  return false;
}

async function waitForPersistedUserTurn(
  page,
  fullPayload,
  firstLine,
  expectedPath,
  before,
  timeoutMs = 60000,
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const state = await page.evaluate(({ fullPayload, firstLine, expectedPath, before }) => {
        const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const currentPath = location.pathname.replace(/\/$/, '');
        const inConversation = location.hostname === 'chatgpt.com' && currentPath.includes('/c/');
        const sameConversation = currentPath === expectedPath;
        const full = normalize(fullPayload);
        const prefix = normalize(firstLine);
        let exactFull = 0;
        let exactPrefixOnly = 0;
        for (const turn of document.querySelectorAll('[data-message-author-role="user"]')) {
          const content = turn.querySelector('[data-testid="collapsible-user-message-content"]')
        || turn.querySelector('.whitespace-pre-wrap') || turn;
      const text = normalize(content.innerText || content.textContent || '');
          if (text === full) exactFull += 1;
          if (text === prefix && text !== full) exactPrefixOnly += 1;
        }
        const persistedExactlyOnce = exactFull === before.exactFull + 1;
        const noPrefixRegression = exactPrefixOnly === before.exactPrefixOnly;
        return { inConversation, sameConversation, persistedExactlyOnce, noPrefixRegression };
      }, { fullPayload, firstLine, expectedPath, before });
      if (
        state.inConversation &&
        state.sameConversation &&
        state.persistedExactlyOnce &&
        state.noPrefixRegression
      ) return true;
    } catch (_) {}
    await sleep(500);
  }
  return false;
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
  // A cached title->URL mapping is not positive route verification. Resolve the
  // exact visible title every time before a verified send; cache only records
  // the most recently observed concrete URL for diagnostics/reuse elsewhere.
  const cache = loadCache();

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


function exactConversationPath(raw) {
  return new URL(validateConcreteUrl(raw)).pathname.replace(/\/$/, '');
}

async function visibleComposer(page) {
  const selector = [
    '[data-testid="prompt-textarea"]',
    '#prompt-textarea',
    'textarea[data-testid="prompt-textarea"]',
  ].join(',');
  try {
    const composer = await page.$(selector);
    if (!composer) return null;
    const visible = await composer.evaluate((el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    });
    return visible ? composer : null;
  } catch (_) {
    return null;
  }
}

async function waitForExactHydratedConversation(page, expectedPath, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  let last = { state: 'SHELL_ONLY_NO_COMPOSER', path: '' };
  while (Date.now() < deadline) {
    try {
      const current = new URL(page.url());
      const currentPath = current.pathname.replace(/\/$/, '');
      if (current.hostname !== 'chatgpt.com') {
        return { ready: false, state: 'AUTH_OR_EXTERNAL_REDIRECT', path: currentPath };
      }
      if (currentPath !== expectedPath) {
        if (currentPath.includes('/c/')) {
          return { ready: false, state: 'ROUTE_MISMATCH', path: currentPath };
        }
        last = { state: 'CONVERSATION_NOT_HYDRATED', path: currentPath };
      } else {
        const composer = await visibleComposer(page);
        if (composer) return { ready: true, state: 'HYDRATED', path: currentPath, composer };
        last = { state: 'SHELL_ONLY_NO_COMPOSER', path: currentPath };
      }
    } catch (_) {
      last = { state: 'CONVERSATION_PROBE_FAILED', path: '' };
    }
    await sleep(250);
  }
  return { ready: false, ...last };
}

async function existingExactHydratedPage(browser, destination) {
  const expectedPath = exactConversationPath(destination);
  for (const candidate of await browser.pages()) {
    let current;
    try { current = new URL(candidate.url()); } catch (_) { continue; }
    if (current.hostname !== 'chatgpt.com' || current.pathname.replace(/\/$/, '') !== expectedPath) continue;
    const state = await waitForExactHydratedConversation(candidate, expectedPath, 2500);
    if (state.ready) return { page: candidate, composer: state.composer, source: 'existing_exact_hydrated' };
  }
  return null;
}

async function openHydratedDestination(browser, destination) {
  const expectedPath = exactConversationPath(destination);
  const attempts = Number(process.env.CHATGPT_HYDRATION_ATTEMPTS || 2);
  const hydrationTimeout = Number(process.env.CHATGPT_HYDRATION_TIMEOUT_MS || 12000);
  for (let attempt = 1; attempt <= Math.max(1, attempts); attempt += 1) {
    const page = await browser.newPage();
    try {
      await page.goto(destination, { waitUntil: 'domcontentloaded', timeout: 30000 });
      let state = await waitForExactHydratedConversation(page, expectedPath, hydrationTimeout);
      if (state.ready) return { page, composer: state.composer, source: `fresh_hydrated_${attempt}` };
      if (state.state === 'SHELL_ONLY_NO_COMPOSER' && attempt === 1) {
        try { await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 }); } catch (_) {}
        state = await waitForExactHydratedConversation(page, expectedPath, hydrationTimeout);
        if (state.ready) return { page, composer: state.composer, source: 'fresh_reload_hydrated' };
      }
      if (['AUTH_OR_EXTERNAL_REDIRECT', 'ROUTE_MISMATCH'].includes(state.state)) {
        throw new Error(`${state.state}: expected ${expectedPath}, got ${state.path}`);
      }
    } finally {
      // Successful returns skip this close. Failed recovery pages are disposable
      // and never replace/navigate an existing hydrated conversation tab.
    }
    try { await page.close(); } catch (_) {}
  }
  throw new Error('SHELL_ONLY_NO_COMPOSER: exact conversation route never hydrated a composer');
}

function classifyWakeDraft(raw, payload, messageId) {
  const text = canonicalComposerText(raw);
  if (!text.trim()) return 'EMPTY';
  const firstLine = String(payload).split(/\r?\n/, 1)[0];
  const wakeIdLine = `WAKE_ID: ${messageId}`;
  const lines = text.split('\n').map((line) => line.trim());
  if (text.trimStart().startsWith(firstLine) && lines.includes(wakeIdLine)) return 'OWNED_STALE_WAKE_DRAFT';
  return 'FOREIGN_DRAFT';
}

async function clearComposerAtomically(page, composer) {
  await page.evaluate((el) => {
    if (!el) throw new Error('composer missing while clearing owned stale wake draft');
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'textarea' || tag === 'input') {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (!setter) throw new Error('native composer value setter unavailable');
      setter.call(el, '');
    } else if (el.isContentEditable) {
      el.replaceChildren();
    } else {
      throw new Error('unsupported ChatGPT composer element');
    }
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
  }, composer);
  if (canonicalComposerText(await composerText(page, composer)).trim()) {
    throw new Error('owned stale wake draft could not be cleared exactly');
  }
}

function sendCommitMarkerPath(messageId) {
  const root = String(process.env.BROWSER_WAKE_SEND_MARKER_DIR || '').trim();
  if (!root) throw new Error('BROWSER_WAKE_SEND_MARKER_DIR is required');
  const digest = createHash('sha256').update(messageId).digest('hex');
  return path.join(root, `${digest}.json`);
}

function writeSendCommitMarker(messageId) {
  const file = sendCommitMarkerPath(messageId);
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify({ message_id: messageId, state: 'SEND_COMMITTED' }) + '\n', { mode: 0o600 });
  fs.renameSync(tmp, file);
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
  let safeToRetry = true;
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

    let destination;
    if (request.destination.kind === 'title') {
      const resolver = await browser.newPage();
      try {
        destination = await resolveByTitle(resolver, request.destination.title);
      } finally {
        try { await resolver.close(); } catch (_) {}
      }
    } else {
      destination = request.destination.url;
    }

    const expectedPath = exactConversationPath(destination);
    let target = await existingExactHydratedPage(browser, destination);
    if (!target) target = await openHydratedDestination(browser, destination);
    const page = target.page;
    let composer = target.composer;
    const finalReadiness = await waitForExactHydratedConversation(page, expectedPath, 2500);
    if (!finalReadiness.ready) {
      throw new Error(`${finalReadiness.state}: target conversation not safely hydrated before idle gate`);
    }
    composer = finalReadiness.composer;

    // Never mutate the composer while the previous assistant task is active.
    // A busy timeout is pre-send and therefore safe for the persistent queue to retry.
    const idle = await waitForStableConversationIdle(page);
    if (!idle.idle) {
      throw new Error('target conversation remained busy before wake insertion');
    }

    if (!composer) throw new Error('SHELL_ONLY_NO_COMPOSER: ChatGPT composer not found');

    const composerBefore = canonicalComposerText(await composerText(page, composer));
    const draftClass = classifyWakeDraft(composerBefore, request.payload, request.messageId);
    if (draftClass === 'OWNED_STALE_WAKE_DRAFT') {
      await clearComposerAtomically(page, composer);
    } else if (draftClass === 'FOREIGN_DRAFT') {
      safeToRetry = false;
      throw new Error('FOREIGN_DRAFT: refusing to overwrite non-orchestrator composer content');
    }

    const wakeFirstLine = String(request.payload).split(/\r?\n/, 1)[0];
    const wakeTurnsBefore = await countWakeTurns(page, request.payload, wakeFirstLine);
    if (wakeTurnsBefore.exactPrefixOnly > 0) {
      safeToRetry = false;
      throw new Error('NAKED_WAKE_PREFIX_PRESENT: refusing automatic send after prefix-only user turn');
    }

    // Insert the entire payload through one DOM text operation. No per-character
    // KeyboardEvent path is allowed because embedded newlines must never submit.
    await setComposerTextAtomically(page, composer, request.payload);
    const insertedPayload = canonicalComposerText(await composerText(page, composer));
    if (insertedPayload !== canonicalComposerText(request.payload)) {
      throw new Error('atomic composer payload readback mismatch before send');
    }

    const wakeTurnsAfterInsertion = await countWakeTurns(page, request.payload, wakeFirstLine);
    if (
      wakeTurnsAfterInsertion.exactFull !== wakeTurnsBefore.exactFull ||
      wakeTurnsAfterInsertion.exactPrefixOnly !== wakeTurnsBefore.exactPrefixOnly
    ) {
      throw new Error('wake user turn appeared before explicit Send');
    }

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

    const assistantTurnsBefore = await page.evaluate(() => {
      const turns = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
      return {
        count: turns.length,
        ids: turns.map((turn) => turn.getAttribute('data-message-id')).filter(Boolean),
      };
    });

    // Persist the commit boundary before the only Send click. A crash before
    // this marker is retry-safe; a crash after it is delivery-uncertain.
    writeSendCommitMarker(request.messageId);
    sendCommitted = true;
    safeToRetry = false;
    await send.click();

    if (!await waitForExactlyOneWakeTurn(
      page,
      request.payload,
      wakeFirstLine,
      wakeTurnsBefore,
    )) {
      throw new Error('post-send wake turn verification failed or prefix-only regression detected');
    }

    // Do not reload while ChatGPT is still producing the response. These pollers
    // deliberately survive transient ChatGPT redirects/navigation context changes.
    // Assistant output content is never scraped.
    if (!await waitForAssistantCompletion(page, assistantTurnsBefore)) {
      throw new Error('ChatGPT response did not finish before persistence check');
    }

    // A newly-rendered user turn can be optimistic UI only. Require persistence
    // across a full reload. ChatGPT may transiently route through `/` before the
    // project conversation hydrates again, so verify across that navigation race.
    try {
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
    } catch (_) {
      // A navigation timeout after Send is uncertain; continue read-only polling.
    }
    if (!await waitForPersistedUserTurn(
      page,
      request.payload,
      wakeFirstLine,
      expectedPath,
      wakeTurnsBefore,
    )) {
      throw new Error('post-send delivery was not persisted exactly once without a prefix-only wake turn');
    }

    process.stdout.write(JSON.stringify({
      status: 'sent',
      message_id: request.messageId,
      delivery_verified: true,
      destination_verified: true,
      persisted_after_reload: true,
      response_completed_before_reload: true,
      target_idle_verified_before_composer_mutation: true,
      atomic_payload_readback_verified: true,
      exactly_one_full_wake_turn_verified: true,
      target_page_source: target.source,
      existing_exact_tab_reused: target.source === 'existing_exact_hydrated',
      verification_source: 'persisted_user_turn_after_reload_same_conversation',
      output_scraped: false,
    }) + '\n');
  } catch (error) {
    fail(error, safeToRetry && !sendCommitted);
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
