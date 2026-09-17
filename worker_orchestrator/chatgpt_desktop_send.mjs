#!/usr/bin/env node

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

function emit(result, code = 0) {
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exitCode = code;
}

function parseRequest(raw) {
  let value;
  try { value = JSON.parse(raw); } catch { throw new Error("invalid JSON request"); }
  for (const key of ["message_id", "destination", "payload"]) {
    if (typeof value?.[key] !== "string" || value[key].trim() === "") {
      throw new Error(`${key} must be a non-empty string`);
    }
  }
  return value;
}

function parseDestination(value) {
  if (value.startsWith("chat-title:")) {
    const title = value.slice("chat-title:".length).trim();
    if (!title || title.length > 512) throw new Error("invalid chat title locator");
    return { kind: "title", title };
  }
  if (value.startsWith("desktop-thread:")) {
    const threadId = value.slice("desktop-thread:".length).trim();
    if (!UUID_RE.test(threadId)) throw new Error("invalid desktop thread id");
    return { kind: "thread", threadId };
  }
  throw new Error("desktop sender requires chat-title: or desktop-thread: locator");
}
function parseDebugUrl(raw) {
  if (!raw) throw new Error("CHATGPT_DESKTOP_DEBUG_URL is required");
  let url;
  try { url = new URL(raw); } catch { throw new Error("invalid desktop debug URL"); }
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error("desktop debug URL must use http or https");
  }
  if (!LOOPBACK_HOSTS.has(url.hostname)) {
    throw new Error("desktop debug URL must be loopback-only");
  }
  if (url.username || url.password) throw new Error("desktop debug URL must not contain credentials");
  if (url.pathname !== "/" || url.search || url.hash) {
    throw new Error("desktop debug URL must be an origin without path, query, or fragment");
  }
  return url.origin;
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function isLikelyShellUrl(raw) {
  if (!raw || raw === "about:blank" || raw.startsWith("devtools://")) return false;
  return !raw.startsWith("http://") && !raw.startsWith("https://");
}

async function pickShellPage(browser) {
  const pages = await browser.pages();
  const candidates = pages.filter((page) => isLikelyShellUrl(page.url()));
  if (candidates.length === 1) return candidates[0];
  if (candidates.length > 1) throw new Error("multiple desktop shell pages found");
  const usable = pages.filter((page) => page.url() !== "about:blank" && !page.url().startsWith("devtools://"));
  if (usable.length === 1) return usable[0];
  throw new Error("desktop shell page could not be resolved uniquely");
}
async function exactTitleMatches(page, title) {
  const selector = [
    "nav a", "aside a", '[role="navigation"] a',
    '[data-testid*="sidebar" i] a', '[class*="sidebar" i] a',
    '[role="dialog"] a', '[role="listbox"] [role="option"]',
    '[role="dialog"] button[data-testid*="conversation"]'
  ].join(",");
  const handles = await page.$$(selector);
  const matches = [];
  for (const handle of handles) {
    const meta = await handle.evaluate((el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return {
        text: (el.textContent ?? "").replace(/\s+/g, " ").trim(),
        visible: style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0,
      };
    });
    if (meta.visible && normalizeText(meta.text) === normalizeText(title)) matches.push(handle);
  }
  return matches;
}

async function uniqueSearchInput(page) {
  const handles = await page.$$('input, textarea, [contenteditable="true"][role="textbox"]');
  const matches = [];
  for (const handle of handles) {
    const meta = await handle.evaluate((el) => {
      const style = getComputedStyle(el), rect = el.getBoundingClientRect();
      const label = [el.getAttribute("placeholder"), el.getAttribute("aria-label"), el.getAttribute("name")]
        .filter(Boolean).join(" ").toLowerCase();
      return { visible: style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0,
        searchLike: label.includes("search") || label.includes("suchen") };
    });
    if (meta.visible && meta.searchLike) matches.push(handle);
  }
  return matches.length === 1 ? matches[0] : null;
}
async function resolveTitle(page, title) {
  let matches = await exactTitleMatches(page, title);
  if (matches.length === 1) return matches[0];
  if (matches.length > 1) throw new Error("chat title is ambiguous in desktop UI");

  await page.keyboard.down(process.platform === "darwin" ? "Meta" : "Control");
  await page.keyboard.press("KeyK");
  await page.keyboard.up(process.platform === "darwin" ? "Meta" : "Control");
  await new Promise((resolve) => setTimeout(resolve, 300));
  const input = await uniqueSearchInput(page);
  if (!input) throw new Error("desktop chat search input not found uniquely");
  await input.click({ clickCount: 3 });
  await page.keyboard.down(process.platform === "darwin" ? "Meta" : "Control");
  await page.keyboard.press("KeyA");
  await page.keyboard.up(process.platform === "darwin" ? "Meta" : "Control");
  await page.keyboard.insertText(title);
  await new Promise((resolve) => setTimeout(resolve, 500));
  matches = await exactTitleMatches(page, title);
  if (matches.length !== 1) {
    throw new Error(matches.length === 0 ? "chat title not found in desktop UI" : "chat title is ambiguous in desktop search");
  }
  return matches[0];
}

async function visibleUnique(page, selectors, purpose) {
  const handles = await page.$$(selectors.join(","));
  const visible = [];
  for (const handle of handles) {
    const yes = await handle.evaluate((el) => {
      const style = getComputedStyle(el), rect = el.getBoundingClientRect();
      return !el.disabled && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    });
    if (yes) visible.push(handle);
  }
  if (visible.length !== 1) throw new Error(`${purpose} not found uniquely`);
  return visible[0];
}
async function fillComposer(page, payload) {
  const composer = await visibleUnique(page, [
    "#prompt-textarea", 'textarea[data-testid*="prompt"]',
    '[contenteditable="true"][role="textbox"][data-testid*="composer"]',
    '[contenteditable="true"][role="textbox"]'
  ], "desktop composer");
  await composer.click();
  await page.keyboard.down(process.platform === "darwin" ? "Meta" : "Control");
  await page.keyboard.press("KeyA");
  await page.keyboard.up(process.platform === "darwin" ? "Meta" : "Control");
  await page.keyboard.insertText(payload);
  const actual = await composer.evaluate((el) => "value" in el ? el.value : (el.textContent ?? ""));
  if (normalizeText(actual) !== normalizeText(payload)) {
    throw new Error("desktop composer verification failed before send");
  }
}

async function findSendButton(page) {
  return visibleUnique(page, [
    'button[data-testid="send-button"]',
    'button[data-testid*="send"]',
    'button[aria-label*="send" i]',
    'button[aria-label*="senden" i]',
    'form button[type="submit"]'
  ], "desktop send button");
}

function selfTest() {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  assert(parseDestination("chat-title:Alpha").title === "Alpha", "title locator");
  assert(parseDestination("desktop-thread:123e4567-e89b-12d3-a456-426614174000").kind === "thread", "thread locator");
  assert(parseDebugUrl("http://127.0.0.1:9223") === "http://127.0.0.1:9223", "loopback debug URL");
  for (const bad of ["https://chatgpt.com/c/x", "chat-title:", "desktop-thread:nope"]) {
    let failed = false; try { parseDestination(bad); } catch { failed = true; }
    assert(failed, `must reject ${bad}`);
  }
  let remoteRejected = false;
  try { parseDebugUrl("http://192.168.1.10:9223"); } catch { remoteRejected = true; }
  assert(remoteRejected, "remote debug endpoint must be rejected");
  process.stdout.write("desktop sender self-test: ok\n");
}
async function main() {
  if (process.argv.includes("--self-test")) { selfTest(); return; }
  const raw = await new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => resolve(data.trim()));
  });
  let committed = false;
  let browser = null;
  try {
    const request = parseRequest(raw);
    const destination = parseDestination(request.destination);
    const browserURL = parseDebugUrl(process.env.CHATGPT_DESKTOP_DEBUG_URL);
    if (destination.kind === "thread") {
      throw new Error("desktop-thread routing is disabled until app-thread mapping is live-verified");
    }
    const puppeteer = await import("puppeteer");
    browser = await puppeteer.default.connect({ browserURL, defaultViewport: null });
    const page = await pickShellPage(browser);
    const target = await resolveTitle(page, destination.title);
    await target.click();
    await new Promise((resolve) => setTimeout(resolve, 400));
    await fillComposer(page, request.payload);
    const sendButton = await findSendButton(page);
    committed = true;
    await sendButton.click();
    emit({ status: "sent", message_id: request.message_id, transport: "chatgpt-desktop-cdp" });
  } catch (error) {
    emit({ status: "error", error: String(error?.message ?? error).slice(0, 240), safe_to_retry: !committed }, committed ? 3 : 2);
  } finally {
    if (browser) await browser.disconnect().catch(() => {});
  }
}

main().catch((error) => {
  emit({ status: "error", error: String(error?.message ?? error).slice(0, 240), safe_to_retry: true }, 2);
});
