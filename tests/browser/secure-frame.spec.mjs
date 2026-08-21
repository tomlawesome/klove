import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { expect, test } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const frameScript = await readFile(
  path.join(root, "src/klove/northbound/assets/secure-frame.js"),
  "utf8",
);
const frameStylesheet = await readFile(
  path.join(root, "src/klove/northbound/assets/secure-frame.css"),
  "utf8",
);
const ownerCredential = "o".repeat(32);

let state;
let parentOrigin;
let frameOrigin;
let attackerOrigin;
let parentServer;
let frameServer;
let attackerServer;

test.beforeAll(async () => {
  state = new FrameState();
  frameServer = await listen(frameHandler);
  frameOrigin = origin(frameServer);
  parentServer = await listen(parentHandler);
  parentOrigin = origin(parentServer);
  attackerServer = await listen(attackerHandler, "localhost");
  attackerOrigin = origin(attackerServer, "localhost");
});

test.afterAll(async () => {
  await Promise.all([parentServer, frameServer, attackerServer].filter(Boolean).map(close));
});

test("runs the exact parent/frame/server nonce handshake without browser persistence", async ({ page }) => {
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");

  const parentNonce = await page.evaluate(() => window.frameTest.parentNonce);
  expect(parentNonce).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(parentNonce).toHaveLength(43);

  await expect(frame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();
  await expect(frame.getByRole("main")).toBeVisible();
  await expect(frame.getByRole("heading", { name: "Set up a printer" })).toBeVisible();
  await expect(frame.getByRole("status")).toBeVisible();
  const credential = frame.getByLabel("Klove owner credential");
  const authorize = frame.getByRole("button", { name: "Authorize this session" });
  await credential.focus();
  await credential.pressSequentially(ownerCredential);
  await credential.press("Tab");
  await expect(authorize).toBeFocused();
  await authorize.press("Enter");

  await expect(frame.getByText("Authorization complete.")).toBeVisible();
  await expect(frame.getByRole("button", { name: "Authorize this session" })).toBeDisabled();
  await expect(page.locator("#parent-status")).toHaveText("authorized");
  expect(state.sessionRequests).toBe(1);

  const privacy = await frame.locator(".frame-shell").evaluate(async () => ({
    localStorage: window.localStorage.length,
    sessionStorage: window.sessionStorage.length,
    serviceWorkers: "serviceWorker" in navigator ? (await navigator.serviceWorker.getRegistrations()).length : 0,
    html: document.documentElement.innerHTML,
    url: window.location.href,
  }));
  expect(privacy.localStorage).toBe(0);
  expect(privacy.sessionStorage).toBe(0);
  expect(privacy.serviceWorkers).toBe(0);
  expect(privacy.html).not.toContain(ownerCredential);
  expect(privacy.url).not.toContain(ownerCredential);
  expect(page.url()).not.toContain(ownerCredential);
});

test("rejects hostile, replayed, and out-of-flow parent/frame messages", async ({ page }) => {
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  await expect(frame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();

  await page.evaluate((expectedOrigin) => {
    window.postMessage(
      {
        version: 1,
        type: "klove.frame.ready",
        operation: "create",
        parent_nonce: "x".repeat(43),
      },
      expectedOrigin,
    );
  }, frameOrigin);
  await expect(page.locator("#parent-status")).toHaveText("waiting");

  await frame.getByLabel("Klove owner credential").fill(ownerCredential);
  await frame.getByRole("button", { name: "Authorize this session" }).click();
  await expect(page.locator("#parent-status")).toHaveText("authorized");
  const before = state.sessionRequests;
  await page.evaluate(() => window.frameTest.sendOutOfFlowChallenge());
  await page.waitForTimeout(100);
  await expect(frame.getByRole("button", { name: "Authorize this session" })).toBeDisabled();
  expect(state.sessionRequests).toBe(before);
});

test("cancellation, expiry, parallel flows, and restart invalidate the proof fail-closed", async ({ browser }) => {
  const first = await browser.newContext();
  const firstPage = await first.newPage();
  await firstPage.goto(parentOrigin);
  const firstFrame = firstPage.frameLocator("#klove-frame");
  await expect(firstFrame.getByRole("button", { name: "Cancel" })).toBeEnabled();
  await firstFrame.getByRole("button", { name: "Cancel" }).focus();
  await firstFrame.getByRole("button", { name: "Cancel" }).press("Enter");
  await expect(firstFrame.getByRole("status")).toHaveText("Cancelled.");
  expect(state.cancelRequests).toBeGreaterThan(0);

  const second = await browser.newContext();
  const secondPage = await second.newPage();
  const third = await browser.newContext();
  const thirdPage = await third.newPage();
  await Promise.all([secondPage.goto(parentOrigin), thirdPage.goto(parentOrigin)]);
  const secondFrame = secondPage.frameLocator("#klove-frame");
  const thirdFrame = thirdPage.frameLocator("#klove-frame");
  await Promise.all([
    expect(secondFrame.getByRole("button", { name: "Authorize this session" })).toBeEnabled(),
    expect(thirdFrame.getByRole("button", { name: "Authorize this session" })).toBeEnabled(),
  ]);
  await Promise.all([
    authorizeFrame(secondFrame),
    authorizeFrame(thirdFrame),
  ]);
  await Promise.all([
    expect(secondPage.locator("#parent-status")).toHaveText("authorized"),
    expect(thirdPage.locator("#parent-status")).toHaveText("authorized"),
  ]);

  const expiry = await browser.newContext();
  const expiryPage = await expiry.newPage();
  await expiryPage.goto(parentOrigin);
  const expiryFrame = expiryPage.frameLocator("#klove-frame");
  await expect(expiryFrame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();
  state.expire();
  await authorizeFrame(expiryFrame);
  await expect(expiryFrame.getByText(/Restart the secure host connection/)).toBeVisible();
  state.restart();

  const restart = await browser.newContext();
  const restartPage = await restart.newPage();
  await restartPage.goto(parentOrigin);
  const restartFrame = restartPage.frameLocator("#klove-frame");
  await expect(restartFrame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();
  state.restart();
  await authorizeFrame(restartFrame);
  await expect(restartFrame.getByText(/Restart the secure host connection/)).toBeVisible();
  await expect(restartFrame.getByRole("button", { name: "Authorize this session" })).toBeDisabled();

  await Promise.all([first.close(), second.close(), third.close(), expiry.close(), restart.close()]);
});

async function authorizeFrame(frame) {
  await frame.getByLabel("Klove owner credential").fill(ownerCredential);
  await frame.getByRole("button", { name: "Authorize this session" }).click();
}

test("refuses top-level and cross-site embedding while remaining keyboard-accessible and responsive", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 640 } });
  const page = await context.newPage();
  await page.goto(`${frameOrigin}/onboarding/setup`);
  await expect(page.getByText("This route requires its configured host application.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Authorize this session" })).toBeDisabled();

  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  const credential = frame.getByLabel("Klove owner credential");
  const authorize = frame.getByRole("button", { name: "Authorize this session" });
  const cancel = frame.getByRole("button", { name: "Cancel" });
  await expect(credential).toBeEnabled();
  await credential.focus();
  await expect(credential).toBeFocused();
  await credential.press("Tab");
  await expect(authorize).toBeFocused();
  await authorize.press("Tab");
  await expect(cancel).toBeFocused();
  const dimensions = await frame.locator(".frame-shell").evaluate(() => ({
    width: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.width);

  await page.goto(attackerOrigin);
  await page.waitForTimeout(150);
  expect(page.frames().some((candidate) => candidate.url().startsWith(frameOrigin))).toBe(false);
  await context.close();
});

class FrameState {
  constructor() {
    this.challenges = new Map();
    this.sessionRequests = 0;
    this.cancelRequests = 0;
    this.sequence = 0;
    this.expired = false;
  }

  issue(parentNonce, operation) {
    const serverNonce = token(++this.sequence);
    this.challenges.set(serverNonce, { parentNonce, operation });
    return serverNonce;
  }

  consume(document) {
    const challenge = this.challenges.get(document.server_nonce);
    this.challenges.delete(document.server_nonce);
    return (
      !this.expired &&
      challenge !== undefined &&
      challenge.parentNonce === document.parent_nonce &&
      challenge.operation === document.operation
    );
  }

  restart() {
    this.challenges.clear();
    this.expired = false;
  }

  expire() {
    this.expired = true;
  }
}

function frameHandler(request, response) {
  const url = new URL(request.url, frameOrigin);
  if (url.pathname === "/onboarding/setup") {
    return send(response, 200, frameDocument(), {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "cross-origin-resource-policy": "same-site",
      "content-security-policy": `default-src 'none'; frame-ancestors ${parentOrigin}; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'`,
    });
  }
  if (url.pathname === "/onboarding/assets/secure-frame.js") {
    return send(response, 200, frameScript, {
      "content-type": "application/javascript; charset=utf-8",
      "cross-origin-resource-policy": "same-site",
    });
  }
  if (url.pathname === "/onboarding/assets/secure-frame.css") {
    return send(response, 200, frameStylesheet, {
      "content-type": "text/css; charset=utf-8",
      "cross-origin-resource-policy": "same-site",
    });
  }
  if (request.method === "OPTIONS" && url.pathname === "/v1/onboarding/frame/challenge") {
    if (
      request.headers.origin === parentOrigin &&
      request.headers["access-control-request-method"] === "POST" &&
      request.headers["access-control-request-headers"]?.toLowerCase() === "content-type"
    ) {
      return send(response, 204, "", {
        "access-control-allow-origin": parentOrigin,
        "access-control-allow-methods": "POST",
        "access-control-allow-headers": "Content-Type",
      });
    }
    return send(response, 403, "denied");
  }
  if (request.method === "POST" && url.pathname === "/v1/onboarding/frame/challenge") {
    return readJson(request).then((document) => {
      if (
        request.headers.origin !== parentOrigin ||
        !matches(document, ["version", "type", "operation", "parent_nonce"]) ||
        document.version !== 1 ||
        document.type !== "klove.frame.challenge" ||
        document.operation !== "create" ||
        !tokenPattern(document.parent_nonce)
      ) {
        return send(response, 403, JSON.stringify({ error: "frame_denied" }), { "content-type": "application/json" });
      }
      const serverNonce = state.issue(document.parent_nonce, document.operation);
      return send(
        response,
        201,
        JSON.stringify({ ...document, server_nonce: serverNonce, expires_in_seconds: 300 }),
        {
          "content-type": "application/json; charset=utf-8",
          "access-control-allow-origin": parentOrigin,
          vary: "Origin",
        },
      );
    });
  }
  if (request.method === "POST" && url.pathname === "/v1/onboarding/frame/session") {
    return readJson(request).then((document) => {
      state.sessionRequests += 1;
      if (
        request.headers.origin !== frameOrigin ||
        !matches(document, ["owner_credential", "operation", "parent_origin", "parent_nonce", "server_nonce"]) ||
        document.parent_origin !== parentOrigin ||
        document.owner_credential !== ownerCredential ||
        !state.consume(document)
      ) {
        return send(response, 403, JSON.stringify({ error: "owner_denied" }), { "content-type": "application/json" });
      }
      return send(
        response,
        201,
        JSON.stringify({ csrf_token: token(501), flow_nonce: token(502), operation: "create", expires_in_seconds: 1800 }),
        {
          "content-type": "application/json; charset=utf-8",
          "set-cookie": "klove_setup=browser-test; Path=/v1/onboarding; HttpOnly; SameSite=Strict",
        },
      );
    });
  }
  if (request.method === "POST" && url.pathname === "/v1/onboarding/frame/cancel") {
    return readJson(request).then((document) => {
      state.cancelRequests += 1;
      if (request.headers.origin !== frameOrigin || !state.consume(document)) {
        return send(response, 403, JSON.stringify({ error: "frame_denied" }), { "content-type": "application/json" });
      }
      return send(response, 200, JSON.stringify({ status: "cancelled" }), { "content-type": "application/json; charset=utf-8" });
    });
  }
  send(response, 404, "not found");
}

function parentHandler(request, response) {
  send(response, 200, parentDocument(), { "content-type": "text/html; charset=utf-8" });
}

function attackerHandler(_request, response) {
  send(response, 200, `<iframe id="hostile" src="${frameOrigin}/onboarding/setup"></iframe>`, { "content-type": "text/html; charset=utf-8" });
}

function frameDocument() {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/onboarding/assets/secure-frame.css"><script src="/onboarding/assets/secure-frame.js" defer></script></head>
<body><main class="frame-shell" data-operation="create" data-parent-origins="[&quot;${parentOrigin}&quot;]"><section class="authorization-card" aria-labelledby="frame-title"><p class="eyebrow">Klove secure connection</p><h1 id="frame-title">Set up a printer</h1><p id="frame-guidance">Waiting for the host application to establish a secure connection.</p><form id="owner-session" novalidate><label for="owner-credential">Klove owner credential</label><input id="owner-credential" type="password" autocomplete="off" inputmode="text" spellcheck="false" maxlength="4096" required disabled><button id="authorize" type="submit" disabled>Authorize this session</button></form><button id="cancel" class="secondary" type="button" disabled>Cancel</button><p id="frame-status" role="status" aria-live="polite"></p><p class="privacy-note">This authorization stays in Klove and is never sent to the host.</p></section></main></body></html>`;
}

function parentDocument() {
  return `<!doctype html>
<html lang="en"><body><p id="parent-status">waiting</p><iframe id="klove-frame" sandbox="allow-scripts allow-forms allow-same-origin"></iframe><script>
(() => {
  const frame = document.querySelector("#klove-frame");
  const frameOrigin = ${JSON.stringify(frameOrigin)};
  const parentNonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32))))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
  const status = document.querySelector("#parent-status");
  const matches = (value, keys) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).sort().join("|") === keys.slice().sort().join("|");
  const token = value => typeof value === "string" && /^[A-Za-z0-9_-]{43}$/.test(value);
  const sendInit = () => frame.contentWindow.postMessage({ version: 1, type: "klove.frame.init", operation: "create", parent_nonce: parentNonce }, frameOrigin);
  const events = [];
  window.frameTest = {
    events,
    parentNonce,
    sendOutOfFlowChallenge() { frame.contentWindow.postMessage({ version: 1, type: "klove.frame.challenge", operation: "create", parent_nonce: parentNonce, server_nonce: "z".repeat(43) }, frameOrigin); },
  };
  window.addEventListener("message", async event => {
    events.push({ source: event.source === frame.contentWindow, origin: event.origin, data: event.data });
    if (event.source !== frame.contentWindow || event.origin !== frameOrigin) return;
    if (matches(event.data, ["version", "type", "operation", "parent_nonce"]) && event.data.version === 1 && event.data.type === "klove.frame.ready" && event.data.operation === "create" && event.data.parent_nonce === parentNonce) {
      const response = await fetch(frameOrigin + "/v1/onboarding/frame/challenge", { method: "POST", mode: "cors", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: 1, type: "klove.frame.challenge", operation: "create", parent_nonce: parentNonce }) });
      const challenge = await response.json();
      if (response.ok && matches(challenge, ["version", "type", "operation", "parent_nonce", "server_nonce", "expires_in_seconds"]) && challenge.parent_nonce === parentNonce && token(challenge.server_nonce)) frame.contentWindow.postMessage({ version: challenge.version, type: challenge.type, operation: challenge.operation, parent_nonce: challenge.parent_nonce, server_nonce: challenge.server_nonce }, frameOrigin);
      return;
    }
    if (matches(event.data, ["version", "type", "operation", "parent_nonce"]) && event.data.version === 1 && event.data.type === "klove.frame.authorized" && event.data.operation === "create" && event.data.parent_nonce === parentNonce) status.textContent = "authorized";
    if (matches(event.data, ["version", "type", "operation", "parent_nonce"]) && event.data.version === 1 && event.data.type === "klove.frame.cancelled" && event.data.operation === "create" && event.data.parent_nonce === parentNonce) status.textContent = "cancelled";
  });
  frame.addEventListener("load", sendInit);
  frame.src = frameOrigin + "/onboarding/setup";
})();
</script></body></html>`;
}

function listen(handler, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const server = createServer(handler);
    server.listen(0, host, () => resolve(server));
  });
}

function close(server) {
  return new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
}

function origin(server, host = "127.0.0.1") {
  return `http://${host}:${server.address().port}`;
}

function send(response, status, body, headers = {}) {
  response.writeHead(status, headers);
  response.end(body);
}

function readJson(request) {
  return new Promise((resolve) => {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      try {
        resolve(JSON.parse(body));
      } catch {
        resolve(null);
      }
    });
  });
}

function matches(value, keys) {
  return value !== null && typeof value === "object" && !Array.isArray(value) && Object.keys(value).sort().join("|") === keys.slice().sort().join("|");
}

function token(value) {
  return value.toString(36).padStart(43, "a").slice(-43);
}

function tokenPattern(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43}$/.test(value);
}
