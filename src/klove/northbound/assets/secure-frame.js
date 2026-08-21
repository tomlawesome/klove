(() => {
  "use strict";

  const VERSION = 1;
  const TOKEN = /^[A-Za-z0-9_-]{43}$/;
  const OPERATIONS = new Set([
    "inspect",
    "create",
    "update",
    "rotate_moonraker",
    "rotate_compatibility",
    "disable",
    "remove",
  ]);
  const root = document.querySelector(".frame-shell");
  const form = document.querySelector("#owner-session");
  const credential = document.querySelector("#owner-credential");
  const authorize = document.querySelector("#authorize");
  const cancel = document.querySelector("#cancel");
  const guidance = document.querySelector("#frame-guidance");
  const status = document.querySelector("#frame-status");

  if (!root || !form || !credential || !authorize || !cancel || !guidance || !status) {
    return;
  }

  const operation = root.dataset.operation;
  const allowedOrigins = parseAllowedOrigins(root.dataset.parentOrigins);
  if (!OPERATIONS.has(operation) || allowedOrigins.length === 0 || window.parent === window) {
    fail("This route requires its configured host application.");
    return;
  }

  const state = {
    parentOrigin: null,
    parentNonce: null,
    serverNonce: null,
    session: null,
    cancelled: false,
    pending: false,
  };

  window.addEventListener("message", receiveParentMessage);
  form.addEventListener("submit", authorizeSession);
  cancel.addEventListener("click", cancelSession);

  function parseAllowedOrigins(value) {
    try {
      const origins = JSON.parse(value || "");
      if (
        !Array.isArray(origins) ||
        origins.length === 0 ||
        origins.length > 32 ||
        origins.some((origin) => typeof origin !== "string" || origin.length === 0)
      ) {
        return [];
      }
      return Object.freeze(origins.slice());
    } catch {
      return [];
    }
  }

  function receiveParentMessage(event) {
    if (event.source !== window.parent || !allowedOrigins.includes(event.origin)) {
      return;
    }
    if (state.parentOrigin === null) {
      if (!matches(event.data, ["version", "type", "operation", "parent_nonce"])) {
        return;
      }
      if (
        event.data.version !== VERSION ||
        event.data.type !== "klove.frame.init" ||
        event.data.operation !== operation ||
        !isToken(event.data.parent_nonce)
      ) {
        return;
      }
      state.parentOrigin = event.origin;
      state.parentNonce = event.data.parent_nonce;
      sendToParent({
        version: VERSION,
        type: "klove.frame.ready",
        operation,
        parent_nonce: state.parentNonce,
      });
      guidance.textContent = "Confirm the secure host connection to continue.";
      return;
    }
    if (
      state.serverNonce !== null ||
      state.session !== null ||
      state.cancelled ||
      state.pending ||
      !matches(event.data, ["version", "type", "operation", "parent_nonce", "server_nonce"])
    ) {
      return;
    }
    if (
      event.data.version !== VERSION ||
      event.data.type !== "klove.frame.challenge" ||
      event.data.operation !== operation ||
      event.data.parent_nonce !== state.parentNonce ||
      !isToken(event.data.server_nonce)
    ) {
      return;
    }
    state.serverNonce = event.data.server_nonce;
    credential.disabled = false;
    authorize.disabled = false;
    cancel.disabled = false;
    guidance.textContent = "Enter the Klove owner credential to authorize this short-lived session.";
    credential.focus();
  }

  async function authorizeSession(event) {
    event.preventDefault();
    if (
      state.pending ||
      state.cancelled ||
      state.parentOrigin === null ||
      state.parentNonce === null ||
      state.serverNonce === null
    ) {
      return;
    }
    const ownerCredential = credential.value;
    if (ownerCredential.length === 0) {
      fail("Enter the owner credential to continue.");
      return;
    }
    state.pending = true;
    setDisabled(true);
    try {
      const responsePromise = fetch("/v1/onboarding/frame/session", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner_credential: ownerCredential,
          operation,
          parent_origin: state.parentOrigin,
          parent_nonce: state.parentNonce,
          server_nonce: state.serverNonce,
        }),
      });
      credential.value = "";
      const response = await responsePromise;
      const grant = await strictJson(response, ["csrf_token", "flow_nonce", "operation", "expires_in_seconds"]);
      if (
        !response.ok ||
        grant === null ||
        grant.operation !== operation ||
        !isToken(grant.csrf_token) ||
        !isToken(grant.flow_nonce) ||
        !Number.isInteger(grant.expires_in_seconds) ||
        grant.expires_in_seconds < 1 ||
        grant.expires_in_seconds > 1800
      ) {
        throw new Error("authorization denied");
      }
      state.session = Object.freeze({ csrfToken: grant.csrf_token, flowNonce: grant.flow_nonce });
      state.serverNonce = null;
      credential.disabled = true;
      authorize.disabled = true;
      cancel.disabled = false;
      guidance.textContent = "This Klove session is authorized. Continue in the secure setup flow.";
      status.textContent = "Authorization complete.";
      sendToParent({
        version: VERSION,
        type: "klove.frame.authorized",
        operation,
        parent_nonce: state.parentNonce,
      });
    } catch {
      state.serverNonce = null;
      fail("Authorization was not accepted. Restart the secure host connection before trying again.");
    } finally {
      state.pending = false;
    }
  }

  async function cancelSession() {
    if (state.pending || state.cancelled) {
      return false;
    }
    state.pending = true;
    setDisabled(true);
    try {
      const endpoint = state.session === null ? "/v1/onboarding/frame/cancel" : "/v1/onboarding/cancel";
      const headers = { "Content-Type": "application/json" };
      const body = state.session === null
        ? {
            operation,
            parent_origin: state.parentOrigin,
            parent_nonce: state.parentNonce,
            server_nonce: state.serverNonce,
          }
        : { flow_nonce: state.session.flowNonce, operation };
      if (state.session !== null) {
        headers["X-Klove-CSRF"] = state.session.csrfToken;
      }
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers,
        body: JSON.stringify(body),
      });
      const result = await strictJson(response, ["status"]);
      if (!response.ok || result === null || result.status !== "cancelled") {
        throw new Error("cancellation denied");
      }
      state.session = null;
      state.serverNonce = null;
      state.cancelled = true;
      guidance.textContent = "The Klove session was cancelled.";
      status.textContent = "Cancelled.";
      sendToParent({
        version: VERSION,
        type: "klove.frame.cancelled",
        operation,
        parent_nonce: state.parentNonce,
      });
      return true;
    } catch {
      fail("Cancellation could not be confirmed. Close this frame and begin a new connection.");
      return false;
    } finally {
      state.pending = false;
    }
  }

  function sendToParent(message) {
    if (state.parentOrigin !== null) {
      window.parent.postMessage(message, state.parentOrigin);
    }
  }

  async function strictJson(response, keys) {
    const contentType = response.headers.get("content-type");
    if (contentType !== "application/json; charset=utf-8") {
      return null;
    }
    try {
      const value = await response.json();
      return matches(value, keys) ? value : null;
    } catch {
      return null;
    }
  }

  function matches(value, keys) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return false;
    }
    const actual = Object.keys(value).sort();
    const expected = keys.slice().sort();
    return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
  }

  function isToken(value) {
    return typeof value === "string" && TOKEN.test(value);
  }

  function setDisabled(disabled) {
    credential.disabled = disabled;
    authorize.disabled = disabled;
    cancel.disabled = disabled;
  }

  function fail(message) {
    setDisabled(true);
    guidance.textContent = message;
    status.textContent = "Connection denied.";
  }
})();
