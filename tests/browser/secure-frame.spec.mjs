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
const moonrakerCredential = "m".repeat(32);
const recoveryUuid = "11111111-1111-4111-8111-111111111111";
const routes = {
  "/": { framePath: "/onboarding/setup", operation: "create", heading: "Register a Klipper printer" },
  "/recovery": { framePath: "/onboarding/recovery", operation: "update", heading: "Repair printer registration" },
  "/recovery/rotate-moonraker": { framePath: "/onboarding/recovery/rotate-moonraker", operation: "rotate_moonraker", heading: "Replace Moonraker credential" },
  "/recovery/rotate-compatibility": { framePath: "/onboarding/recovery/rotate-compatibility", operation: "rotate_compatibility", heading: "Rotate compatibility access" },
  "/recovery/disable": { framePath: "/onboarding/recovery/disable", operation: "disable", heading: "Disable a printer" },
  "/recovery/remove": { framePath: "/onboarding/recovery/remove", operation: "remove", heading: "Remove a disabled printer" },
};

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

test("creates one direct-probed printer without browser persistence or a completion handoff", async ({ page }) => {
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  const parentNonce = await page.evaluate(() => window.frameTest.parentNonce);
  expect(parentNonce).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect(parentNonce).toHaveLength(43);
  await expect(frame.getByRole("heading", { name: "Register a Klipper printer" })).toBeVisible();

  await authorizeFrame(frame);
  await expect(frame.getByRole("status")).toHaveText("Secure owner session active.");
  await configureCreate(frame, { profile: true, dispatch: true });
  await frame.getByRole("button", { name: "Probe and register" }).click();
  await expect(frame.getByRole("status")).toHaveText("Registration operation confirmed.");
  await expect(frame.getByText("Klove confirmed the bounded registration operation")).toBeVisible();
  await expect(page.locator("#parent-status")).toHaveText("authorized");
  expect(state.operations).toContain("create");
  expect(state.paths).not.toContain("/v1/onboarding/inspect");
  expect(frameScript).not.toContain("klove.frame.complete");
  expect(frameScript).not.toContain("access_code");
  await expect(frame.getByRole("heading", { name: "Confirm registration details" })).toBeVisible();

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
  expect(privacy.html).not.toContain(moonrakerCredential);
  expect(privacy.url).not.toContain(ownerCredential);
  expect(privacy.url).not.toContain(moonrakerCredential);
  expect(page.url()).not.toContain(ownerCredential);
  expect(page.url()).not.toContain(moonrakerCredential);
});

test("keeps a retryable direct-probe error bounded and clears submitted credentials", async ({ page }) => {
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  await authorizeFrame(frame);
  await configureCreate(frame);
  state.failNextLifecycle("probe_failed");
  await frame.getByRole("button", { name: "Probe and register" }).click();
  await expect(frame.getByText(/Retry the same exact details/)).toBeVisible();
  await expect(frame.getByLabel("Moonraker credential")).toHaveValue("");
  await frame.getByLabel("Moonraker credential").fill(moonrakerCredential);
  await frame.getByRole("button", { name: "Probe and register" }).click();
  await expect(frame.getByRole("status")).toHaveText("Registration operation confirmed.");
  expect(state.lifecycleRequests).toBeGreaterThanOrEqual(3);
});

test("rejects hostile lifecycle fields locally while preserving the exact session for cancellation", async ({ page }) => {
  const lifecycleBefore = state.lifecycleRequests;
  const pathsBefore = state.paths.length;
  await page.goto(`${parentOrigin}/recovery`);
  const update = page.frameLocator("#klove-frame");
  await authorizeFrame(update);
  const updateSubmit = update.getByRole("button", { name: "Probe and repair registration" });
  const registeredUuid = update.getByLabel("Registered printer UUID");
  const revision = update.getByLabel("Current registration revision");

  await registeredUuid.fill("not-a-printer-uuid");
  await revision.fill("not-a-revision");
  await update.getByLabel("Printer name").fill("Recovered printer");
  await update.getByLabel("Moonraker origin").fill("https://printer.example.test:7125");
  await updateSubmit.click();
  await expect(update.getByText("Enter one canonical version 4 printer UUID.")).toBeVisible();
  expect(state.lifecycleRequests).toBe(lifecycleBefore);
  expect(state.paths).toHaveLength(pathsBefore);

  await registeredUuid.fill(recoveryUuid);
  await updateSubmit.click();
  await expect(update.getByText("Enter one positive whole-number registration value.")).toBeVisible();
  expect(state.lifecycleRequests).toBe(lifecycleBefore);

  await revision.fill("1");
  await update.getByLabel("Printer name").evaluate((input) => {
    input.value = "Recovered\u007fprinter";
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await updateSubmit.click();
  await expect(
    update.getByText("Enter exact bounded text without surrounding whitespace or control characters."),
  ).toBeVisible();
  expect(state.lifecycleRequests).toBe(lifecycleBefore);

  await update.getByLabel("Printer name").fill("Recovered printer");
  await update.getByLabel("Moonraker origin").fill("https://printer.example.test/not-an-origin");
  await updateSubmit.click();
  await expect(update.getByText("Enter one exact canonical HTTP(S) Moonraker origin.")).toBeVisible();
  expect(state.lifecycleRequests).toBe(lifecycleBefore);

  await update.getByLabel("Moonraker origin").fill("https://printer.example.test:7125");
  await update.getByLabel("Bind one exact Klipper safety profile").check();
  await update.getByLabel("Safety-profile generation").fill("0");
  await updateSubmit.click();
  await expect(update.getByText("Enter one positive whole-number registration value.")).toBeVisible();
  expect(state.lifecycleRequests).toBe(lifecycleBefore);
  await expect(update.getByRole("button", { name: "Cancel" })).toBeEnabled();
  await update.getByRole("button", { name: "Cancel" }).click();
  await expect(update.getByRole("status")).toHaveText("Cancelled.");
  expect(state.lifecycleRequests).toBe(lifecycleBefore);
  expect(state.paths).toHaveLength(pathsBefore);

  await page.goto(`${parentOrigin}/recovery/rotate-moonraker`);
  const rotate = page.frameLocator("#klove-frame");
  await authorizeFrame(rotate);
  await rotate.getByLabel("Registered printer UUID").fill(recoveryUuid);
  await rotate.getByLabel("Current registration revision").fill("1");
  await rotate.getByLabel("Replacement Moonraker credential").fill("short");
  await rotate.getByRole("button", { name: "Probe and replace credential" }).click();
  await expect(rotate.getByText("Enter the complete visible-ASCII Moonraker credential.")).toBeVisible();
  await expect(rotate.getByLabel("Replacement Moonraker credential")).toHaveValue("");
  expect(state.lifecycleRequests).toBe(lifecycleBefore);
  expect(state.paths).toHaveLength(pathsBefore);

  await rotate.getByLabel("Replacement Moonraker credential").fill(moonrakerCredential);
  await rotate.getByRole("button", { name: "Cancel" }).click();
  await expect(rotate.getByRole("status")).toHaveText("Cancelled.");
  const privacy = await rotate.locator("body").evaluate(() => ({
    html: document.documentElement.innerHTML,
    url: window.location.href,
  }));
  expect(privacy.html).not.toContain(moonrakerCredential);
  expect(privacy.url).not.toContain(moonrakerCredential);
  expect(state.lifecycleRequests).toBe(lifecycleBefore);
  expect(state.paths).toHaveLength(pathsBefore);
});

test("runs each recovery operation as one separately authorized exact lifecycle action", async ({ browser }) => {
  for (const [parentPath, route] of Object.entries(routes).filter(([path]) => path !== "/")) {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(`${parentOrigin}${parentPath}`);
    const frame = page.frameLocator("#klove-frame");
    await expect(frame.getByRole("heading", { name: route.heading })).toBeVisible();
    await authorizeFrame(frame);
    await configureRecovery(frame, route.operation);
    const button = frame.getByRole("button", { name: buttonFor(route.operation) });
    await button.click();
    await expect(frame.getByRole("status")).toHaveText("Registration operation confirmed.");
    expect(state.operations).toContain(route.operation);
    await context.close();
  }
});

test("rejects hostile messages and preserves cancellation and restart fail-closed behavior", async ({ browser, page }) => {
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  await page.evaluate((expectedOrigin) => {
    window.postMessage(
      { version: 1, type: "klove.frame.ready", operation: "create", parent_nonce: "x".repeat(43) },
      expectedOrigin,
    );
  }, frameOrigin);
  await expect(page.locator("#parent-status")).toHaveText("waiting");
  await authorizeFrame(frame);
  const requestsBefore = state.lifecycleRequests;
  await page.evaluate(() => window.frameTest.sendOutOfFlowChallenge());
  await page.waitForTimeout(100);
  await expect(frame.getByRole("button", { name: "Probe and register" })).toBeEnabled();
  expect(state.lifecycleRequests).toBe(requestsBefore);
  await configureCreate(frame);
  await frame.getByRole("button", { name: "Cancel" }).click();
  await expect(frame.getByRole("status")).toHaveText("Cancelled.");
  await expect(frame.locator("#lifecycle-panel")).toBeHidden();
  const cancelledPrivacy = await frame.locator("body").evaluate(() => ({
    owner: document.querySelector("#owner-credential")?.value,
    html: document.documentElement.innerHTML,
  }));
  expect(cancelledPrivacy.owner).toBe("");
  expect(cancelledPrivacy.html).not.toContain(ownerCredential);
  expect(cancelledPrivacy.html).not.toContain(moonrakerCredential);

  const restart = await browser.newContext();
  const restartPage = await restart.newPage();
  await restartPage.goto(parentOrigin);
  const restartFrame = restartPage.frameLocator("#klove-frame");
  await expect(restartFrame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();
  state.restart();
  await restartFrame.getByLabel("Klove owner credential").fill(ownerCredential);
  await restartFrame.getByRole("button", { name: "Authorize this session" }).click();
  await expect(restartFrame.getByText(/Restart the secure host connection/)).toBeVisible();
  await restart.close();
});

test("refuses top-level and cross-site embedding while remaining keyboard-accessible and responsive", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 320, height: 640 } });
  const page = await context.newPage();
  await page.goto(`${frameOrigin}/onboarding/setup`);
  await expect(page.getByText("This route requires its configured host application.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Authorize this session" })).toBeDisabled();

  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  await authorizeFrame(frame);
  const printerUuid = frame.getByLabel("Printer UUID");
  await printerUuid.focus();
  await expect(printerUuid).toBeFocused();
  await printerUuid.press("Tab");
  await expect(frame.getByLabel("Printer name")).toBeFocused();
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

test("preflights secure UUID support before granting an owner session", async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => {
    Object.defineProperty(globalThis.crypto, "randomUUID", { configurable: true, value: undefined });
  });
  const page = await context.newPage();
  const sessionsBefore = state.ownerSessionRequests;
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  await expect(frame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();
  await frame.getByLabel("Klove owner credential").fill(ownerCredential);
  await frame.getByRole("button", { name: "Authorize this session" }).click();
  await expect(frame.getByText("This browser cannot create a secure registration identifier.")).toBeVisible();
  await expect(frame.getByLabel("Klove owner credential")).toHaveValue("");
  expect(state.ownerSessionRequests).toBe(sessionsBefore);
  await context.close();
});

test("discards sensitive form DOM when cancellation outcome is uncertain", async ({ page }) => {
  await page.goto(parentOrigin);
  const frame = page.frameLocator("#klove-frame");
  await authorizeFrame(frame);
  await configureCreate(frame);
  state.failNextCancellation();
  await frame.getByRole("button", { name: "Cancel" }).click();
  await expect(frame.getByText(/Cancellation could not be confirmed/)).toBeVisible();
  await expect(frame.locator("#lifecycle-panel")).toBeHidden();
  const privacy = await frame.locator("body").evaluate(() => ({
    owner: document.querySelector("#owner-credential")?.value,
    html: document.documentElement.innerHTML,
  }));
  expect(privacy.owner).toBe("");
  expect(privacy.html).not.toContain(ownerCredential);
  expect(privacy.html).not.toContain(moonrakerCredential);
});

async function authorizeFrame(frame) {
  await expect(frame.getByRole("button", { name: "Authorize this session" })).toBeEnabled();
  await frame.getByLabel("Klove owner credential").fill(ownerCredential);
  await frame.getByRole("button", { name: "Authorize this session" }).click();
  await expect(frame.getByRole("status")).toHaveText("Secure owner session active.");
}

async function configureCreate(frame, options = {}) {
  await frame.getByLabel("Printer name").fill("Workshop printer");
  await frame.getByLabel("Moonraker origin").fill("https://printer.example.test:7125");
  await frame.getByLabel("Moonraker credential").fill(moonrakerCredential);
  if (options.profile) {
    await frame.getByLabel("Bind one exact Klipper safety profile").check();
    await frame.getByLabel("Safety-profile generation").fill("1");
    await frame.getByLabel("Registered slicer profile ID").fill("klipper-voron-24-0.4");
    await frame.getByLabel("Nozzle diameter (micrometres)").fill("400");
    await frame.getByLabel("Build width (micrometres)").fill("350000");
    await frame.getByLabel("Build depth (micrometres)").fill("350000");
    await frame.getByLabel("Build height (micrometres)").fill("350000");
    await frame.getByLabel("Build plate ID").fill("textured-pei");
  }
  if (options.dispatch) {
    await frame.getByLabel("Opt in to Klove job controls after positive direct capability evidence").check();
    await frame.getByLabel("Opt in to Klove dispatch after positive direct capability evidence").check();
  }
}

async function configureRecovery(frame, operation) {
  await frame.getByLabel("Registered printer UUID").fill(recoveryUuid);
  await frame.getByLabel("Current registration revision").fill("1");
  if (operation === "update") {
    await frame.getByLabel("Printer name").fill("Recovered printer");
    await frame.getByLabel("Moonraker origin").fill("https://printer.example.test:7125");
  } else if (operation === "rotate_moonraker") {
    await frame.getByLabel("Replacement Moonraker credential").fill(moonrakerCredential);
  } else if (operation === "rotate_compatibility") {
    await frame.getByLabel("I understand this invalidates the existing compatibility access for this printer.").check();
  } else if (operation === "disable") {
    await frame.getByLabel("I understand this disables Klove control and dispatch for this printer.").check();
  } else if (operation === "remove") {
    await frame.getByLabel("I understand this removes only an already disabled Klove registration.").check();
  }
}

function buttonFor(operation) {
  return {
    update: "Probe and repair registration",
    rotate_moonraker: "Probe and replace credential",
    rotate_compatibility: "Rotate compatibility access",
    disable: "Disable printer",
    remove: "Remove disabled printer",
  }[operation];
}

class FrameState {
  constructor() {
    this.challenges = new Map();
    this.sessions = new Map();
    this.operations = [];
    this.paths = [];
    this.sequence = 0;
    this.lifecycleRequests = 0;
    this.ownerSessionRequests = 0;
    this.expired = false;
    this.failure = null;
    this.cancellationFailure = false;
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

  issueSession(operation) {
    this.ownerSessionRequests += 1;
    const csrfToken = token(++this.sequence);
    const flowNonce = token(++this.sequence);
    this.sessions.set(flowNonce, { csrfToken, operation });
    return { csrfToken, flowNonce };
  }

  session(document, headers, operation) {
    const session = this.sessions.get(document.flow_nonce);
    return (
      session !== undefined &&
      session.operation === operation &&
      session.csrfToken === headers["x-klove-csrf"]
    );
  }

  complete(document, headers, operation) {
    if (!this.session(document, headers, operation) || !validLifecycleDocument(document, headers, operation)) {
      return null;
    }
    this.lifecycleRequests += 1;
    if (this.failure !== null) {
      const error = this.failure;
      this.failure = null;
      return { error };
    }
    this.sessions.delete(document.flow_nonce);
    this.operations.push(operation);
    return { printer: publicPrinter(document.printer_uuid, operation) };
  }

  cancel(document, headers) {
    if (this.cancellationFailure) {
      this.cancellationFailure = false;
      return "uncertain";
    }
    if (!this.session(document, headers, document.operation)) {
      return false;
    }
    this.sessions.delete(document.flow_nonce);
    return true;
  }

  failNextLifecycle(code) {
    this.failure = code;
  }

  failNextCancellation() {
    this.cancellationFailure = true;
  }

  restart() {
    this.challenges.clear();
    this.sessions.clear();
    this.expired = false;
  }
}

function frameHandler(request, response) {
  const url = new URL(request.url, frameOrigin);
  const route = Object.values(routes).find((candidate) => candidate.framePath === url.pathname);
  if (route) {
    return send(response, 200, frameDocument(route), documentHeaders());
  }
  if (url.pathname === "/onboarding/assets/secure-frame.js") {
    return send(response, 200, frameScript, { "content-type": "application/javascript; charset=utf-8" });
  }
  if (url.pathname === "/onboarding/assets/secure-frame.css") {
    return send(response, 200, frameStylesheet, { "content-type": "text/css; charset=utf-8" });
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
    return json(response, 403, { error: "frame_denied" });
  }
  if (request.method === "POST" && url.pathname === "/v1/onboarding/frame/challenge") {
    return readJson(request).then((document) => {
      if (
        request.headers.origin !== parentOrigin ||
        !matches(document, ["version", "type", "operation", "parent_nonce"]) ||
        document.version !== 1 ||
        document.type !== "klove.frame.challenge" ||
        !operationKnown(document.operation) ||
        !tokenPattern(document.parent_nonce)
      ) {
        return json(response, 403, { error: "frame_denied" });
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
      if (
        request.headers.origin !== frameOrigin ||
        !matches(document, ["owner_credential", "operation", "parent_origin", "parent_nonce", "server_nonce"]) ||
        document.parent_origin !== parentOrigin ||
        document.owner_credential !== ownerCredential ||
        !state.consume(document)
      ) {
        return json(response, 403, { error: "owner_denied" });
      }
      const session = state.issueSession(document.operation);
      return send(
        response,
        201,
        JSON.stringify({ csrf_token: session.csrfToken, flow_nonce: session.flowNonce, operation: document.operation, expires_in_seconds: 1800 }),
        {
          "content-type": "application/json; charset=utf-8",
          "set-cookie": "klove_setup=browser-test; Path=/v1/onboarding; HttpOnly; SameSite=Strict",
        },
      );
    });
  }
  if (request.method === "POST" && url.pathname === "/v1/onboarding/frame/cancel") {
    return readJson(request).then((document) => {
      if (request.headers.origin !== frameOrigin || !state.consume(document)) {
        return json(response, 403, { error: "frame_denied" });
      }
      return json(response, 200, { status: "cancelled" });
    });
  }
  if (request.method === "POST" && url.pathname === "/v1/onboarding/cancel") {
    return readJson(request).then((document) => {
      if (request.headers.origin !== frameOrigin || !matches(document, ["flow_nonce", "operation"])) {
        return json(response, 403, { error: "owner_denied" });
      }
      const cancellation = state.cancel(document, request.headers);
      if (cancellation !== true) {
        return json(response, 403, { error: "owner_denied" });
      }
      return json(response, 200, { status: "cancelled" });
    });
  }
  const operation = lifecycleOperation(request.method, url.pathname);
  if (operation !== null) {
    return readJson(request).then((document) => {
      state.paths.push(url.pathname);
      if (
        request.headers.origin !== frameOrigin ||
        !isObject(document) ||
        document.flow_nonce === undefined
      ) {
        return json(response, 403, { error: "owner_denied" });
      }
      const result = state.complete(document, request.headers, operation);
      if (result === null) {
        return json(response, 403, { error: "owner_denied" });
      }
      if (result.error) {
        return json(response, 422, result);
      }
      return json(response, operation === "create" ? 201 : 200, result, {
        "set-cookie": "klove_setup=; Path=/v1/onboarding; Max-Age=0",
      });
    });
  }
  send(response, 404, "not found");
}

function parentHandler(request, response) {
  const path = new URL(request.url, parentOrigin).pathname;
  send(response, 200, parentDocument(routes[path] || routes["/"]), { "content-type": "text/html; charset=utf-8" });
}

function attackerHandler(_request, response) {
  send(response, 200, `<iframe id="hostile" src="${frameOrigin}/onboarding/setup"></iframe>`, { "content-type": "text/html; charset=utf-8" });
}

function frameDocument(route) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/onboarding/assets/secure-frame.css"><script src="/onboarding/assets/secure-frame.js" defer></script></head>
<body><main class="frame-shell" data-operation="${route.operation}" data-parent-origins="[&quot;${parentOrigin}&quot;]"><section class="authorization-card" aria-labelledby="frame-title"><p class="eyebrow">Klove secure connection</p><h1 id="frame-title">${route.heading}</h1><p id="frame-guidance">Waiting for the host application to establish a secure connection.</p><form id="owner-session" novalidate><label for="owner-credential">Klove owner credential</label><input id="owner-credential" type="password" autocomplete="off" inputmode="text" spellcheck="false" maxlength="4096" required disabled><button id="authorize" type="submit" disabled>Authorize this session</button></form><section id="lifecycle-panel" hidden aria-labelledby="lifecycle-title"><h2 id="lifecycle-title">Confirm registration details</h2><p id="lifecycle-guidance"></p></section><button id="cancel" class="secondary" type="button" disabled>Cancel</button><p id="frame-status" role="status" aria-live="polite"></p><p class="privacy-note">This authorization stays in Klove and is never sent to the host.</p></section></main></body></html>`;
}

function parentDocument(route) {
  return `<!doctype html>
<html lang="en"><body><p id="parent-status">waiting</p><iframe id="klove-frame" sandbox="allow-scripts allow-forms allow-same-origin"></iframe><script>
(() => {
  const frame = document.querySelector("#klove-frame");
  const frameOrigin = ${JSON.stringify(frameOrigin)};
  const operation = ${JSON.stringify(route.operation)};
  const route = ${JSON.stringify(route.framePath)};
  const parentNonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32))))
    .replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  const status = document.querySelector("#parent-status");
  const matches = (value, keys) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).sort().join("|") === keys.slice().sort().join("|");
  const token = value => typeof value === "string" && /^[A-Za-z0-9_-]{43}$/.test(value);
  const sendInit = () => frame.contentWindow.postMessage({ version: 1, type: "klove.frame.init", operation, parent_nonce: parentNonce }, frameOrigin);
  window.frameTest = {
    parentNonce,
    sendOutOfFlowChallenge() { frame.contentWindow.postMessage({ version: 1, type: "klove.frame.challenge", operation, parent_nonce: parentNonce, server_nonce: "z".repeat(43) }, frameOrigin); },
  };
  window.addEventListener("message", async event => {
    if (event.source !== frame.contentWindow || event.origin !== frameOrigin) return;
    if (matches(event.data, ["version", "type", "operation", "parent_nonce"]) && event.data.version === 1 && event.data.type === "klove.frame.ready" && event.data.operation === operation && event.data.parent_nonce === parentNonce) {
      const response = await fetch(frameOrigin + "/v1/onboarding/frame/challenge", { method: "POST", mode: "cors", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: 1, type: "klove.frame.challenge", operation, parent_nonce: parentNonce }) });
      const challenge = await response.json();
      if (response.ok && matches(challenge, ["version", "type", "operation", "parent_nonce", "server_nonce", "expires_in_seconds"]) && challenge.operation === operation && challenge.parent_nonce === parentNonce && token(challenge.server_nonce)) frame.contentWindow.postMessage({ version: challenge.version, type: challenge.type, operation: challenge.operation, parent_nonce: challenge.parent_nonce, server_nonce: challenge.server_nonce }, frameOrigin);
      return;
    }
    if (matches(event.data, ["version", "type", "operation", "parent_nonce"]) && event.data.version === 1 && event.data.type === "klove.frame.authorized" && event.data.operation === operation && event.data.parent_nonce === parentNonce) status.textContent = "authorized";
    if (matches(event.data, ["version", "type", "operation", "parent_nonce"]) && event.data.version === 1 && event.data.type === "klove.frame.cancelled" && event.data.operation === operation && event.data.parent_nonce === parentNonce) status.textContent = "cancelled";
  });
  frame.addEventListener("load", sendInit);
  frame.src = frameOrigin + route;
})();
</script></body></html>`;
}

function lifecycleOperation(method, path) {
  if (method === "POST" && path === "/v1/onboarding/printers") return "create";
  const match = path.match(/^\/v1\/onboarding\/printers\/([0-9a-f-]+)(?:\/(rotate\/moonraker|rotate\/compatibility|disable))?$/);
  if (!match) return null;
  if (method === "PUT" && match[2] === undefined) return "update";
  if (method === "DELETE" && match[2] === undefined) return "remove";
  if (method === "POST" && match[2] === "rotate/moonraker") return "rotate_moonraker";
  if (method === "POST" && match[2] === "rotate/compatibility") return "rotate_compatibility";
  if (method === "POST" && match[2] === "disable") return "disable";
  return null;
}

function publicPrinter(printerUuid, operation) {
  return {
    printer_uuid: typeof printerUuid === "string" ? printerUuid : recoveryUuid,
    revision: 1,
    lifecycle: operation === "remove" ? "removed" : operation === "disable" ? "disabled" : "active",
    identity: { klipper_hostname: "verified-klipper" },
  };
}

function documentHeaders() {
  return {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "cross-origin-resource-policy": "same-site",
    "content-security-policy": `default-src 'none'; frame-ancestors ${parentOrigin}; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'`,
  };
}

function operationKnown(value) {
  return Object.values(routes).some((route) => route.operation === value);
}

function validLifecycleDocument(document, headers, operation) {
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(headers["idempotency-key"] || "") ||
    !tokenPattern(document.flow_nonce)
  ) {
    return false;
  }
  const expected = {
    create: ["control_enabled", "dispatch_enabled", "display_name", "endpoint", "flow_nonce", "moonraker_credential", "printer_uuid", "safety_profiles"],
    update: ["control_enabled", "dispatch_enabled", "display_name", "endpoint", "expected_revision", "flow_nonce", "reactivate", "safety_profiles"],
    rotate_moonraker: ["expected_revision", "flow_nonce", "moonraker_credential"],
    rotate_compatibility: ["expected_revision", "flow_nonce"],
    disable: ["expected_revision", "flow_nonce"],
    remove: ["expected_revision", "flow_nonce"],
  }[operation];
  if (!matches(document, expected)) {
    return false;
  }
  if (operation === "create") {
    return validEndpoint(document.endpoint) && validProfiles(document.safety_profiles, document.printer_uuid);
  }
  if (operation === "update") {
    return validEndpoint(document.endpoint) && validProfiles(document.safety_profiles, recoveryUuid);
  }
  return Number.isInteger(document.expected_revision) && document.expected_revision > 0;
}

function validEndpoint(endpoint) {
  return (
    matches(endpoint, ["allow_insecure_http", "url", "verify_tls"]) &&
    typeof endpoint.url === "string" &&
    typeof endpoint.allow_insecure_http === "boolean" &&
    typeof endpoint.verify_tls === "boolean"
  );
}

function validProfiles(profiles, printerUuid) {
  if (!Array.isArray(profiles)) {
    return false;
  }
  if (profiles.length === 0) {
    return true;
  }
  return profiles.length === 1 && profiles[0]?.printer_uuid === printerUuid;
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

function json(response, status, body, headers = {}) {
  send(response, status, JSON.stringify(body), { "content-type": "application/json; charset=utf-8", ...headers });
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
  return isObject(value) && Object.keys(value).sort().join("|") === keys.slice().sort().join("|");
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function token(value) {
  return value.toString(36).padStart(43, "a").slice(-43);
}

function tokenPattern(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43}$/.test(value);
}
