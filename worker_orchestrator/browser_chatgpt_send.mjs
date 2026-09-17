#!/usr/bin/env node

import fs from 'node:fs';
import process from 'node:process';
import puppeteer from 'puppeteer';

function fail(error, safeToRetry, code = 2) {
  process.stdout.write(JSON.stringify({ status: 'failed', error: String(error).slice(0, 240), safe_to_retry: !!safeToRetry }) + '\n');
  process.exit(code);
}

function validateDestination(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.hostname !== 'chatgpt.com' || url.port || url.username || url.password) {
    throw new Error('destination must be a credential-free https://chatgpt.com URL');
  }
  const path = url.pathname.replace(/\/$/, '');
  if (!/^\/(?:g\/g-p-[A-Za-z0-9_-]+\/)?c\/[A-Za-z0-9-]+$/.test(path)) {
    throw new Error('destination must identify one concrete ChatGPT conversation');
  }
  return url.toString();
}

async function readRequest() {
  const raw = fs.readFileSync(0, 'utf8').trim();
  if (!raw) throw new Error('empty request');
  const value = JSON.parse(raw);
  const messageId = String(value.message_id || '').trim();
  const destination = validateDestination(String(value.destination || '').trim());
  const payload = String(value.payload || '');
  if (!messageId || !payload.trim()) throw new Error('message_id and payload are required');
  if (payload.length > 20000) throw new Error('payload exceeds browser wake size limit');
  return { messageId, destination, payload };
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
    await page.goto(request.destination, { waitUntil: 'domcontentloaded', timeout: 30000 });

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
