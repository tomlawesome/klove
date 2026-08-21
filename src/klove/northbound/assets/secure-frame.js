(() => {
  "use strict";

  const VERSION = 1;
  const TOKEN = /^[A-Za-z0-9_-]{43}$/;
  const ACCESS_CODE = /^[A-Za-z0-9_-]{20}$/;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const PROXY_SERIAL = /^KLOVE-[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}$/;
  const DNS_HOST = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;
  const INTEGER = /^[1-9][0-9]{0,15}$/;
  const VISIBLE_SECRET = /^[\x21-\x7e]{32,4096}$/;
  const MAX_SAFE_LIFECYCLE_INTEGER = Number.MAX_SAFE_INTEGER;
  const OPERATIONS = new Set([
    "create",
    "update",
    "rotate_moonraker",
    "rotate_compatibility",
    "disable",
    "remove",
  ]);
  const RETRYABLE_ERRORS = new Set([
    "conflict",
    "busy",
    "stale_revision",
    "invalid_transition",
    "probe_failed",
    "printer_fenced",
    "fence_unavailable",
  ]);
  const ACTIONS = Object.freeze({
    create: {
      method: "POST",
      path: () => "/v1/onboarding/printers",
      button: "Probe and register",
      guidance: "Enter a discovered Moonraker origin or a manual exact origin. Klove will probe it directly before saving anything.",
    },
    update: {
      method: "PUT",
      path: (printerUuid) => `/v1/onboarding/printers/${printerUuid}`,
      button: "Probe and repair registration",
      guidance: "Supply the exact registered UUID and revision. Klove will use the stored credential for a direct probe before replacing the binding.",
    },
    rotate_moonraker: {
      method: "POST",
      path: (printerUuid) => `/v1/onboarding/printers/${printerUuid}/rotate/moonraker`,
      button: "Probe and replace credential",
      guidance: "Supply the exact printer UUID, revision, and replacement credential. Klove probes it directly before committing the rotation.",
    },
    rotate_compatibility: {
      method: "POST",
      path: (printerUuid) => `/v1/onboarding/printers/${printerUuid}/rotate/compatibility`,
      button: "Rotate compatibility access",
      guidance: "Supply the exact printer UUID and revision. The replacement compatibility value remains private to Klove and is never shown here.",
    },
    disable: {
      method: "POST",
      path: (printerUuid) => `/v1/onboarding/printers/${printerUuid}/disable`,
      button: "Disable printer",
      guidance: "Disablement requires the exact active printer UUID and revision. Klove turns its control and dispatch opt-ins off.",
    },
    remove: {
      method: "DELETE",
      path: (printerUuid) => `/v1/onboarding/printers/${printerUuid}`,
      button: "Remove disabled printer",
      guidance: "Removal requires the exact disabled printer UUID and revision. Klove retains only its canonical tombstone.",
    },
  });
  const root = document.querySelector(".frame-shell");
  const ownerForm = document.querySelector("#owner-session");
  const credential = document.querySelector("#owner-credential");
  const authorize = document.querySelector("#authorize");
  const cancel = document.querySelector("#cancel");
  const guidance = document.querySelector("#frame-guidance");
  const status = document.querySelector("#frame-status");
  const lifecyclePanel = document.querySelector("#lifecycle-panel");
  const lifecycleTitle = document.querySelector("#lifecycle-title");
  const lifecycleGuidance = document.querySelector("#lifecycle-guidance");

  if (
    !root ||
    !ownerForm ||
    !credential ||
    !authorize ||
    !cancel ||
    !guidance ||
    !status ||
    !lifecyclePanel ||
    !lifecycleTitle ||
    !lifecycleGuidance
  ) {
    return;
  }

  const operation = root.dataset.operation;
  const action = ACTIONS[operation];
  const allowedOrigins = parseAllowedOrigins(root.dataset.parentOrigins);
  const state = {
    parentOrigin: null,
    parentNonce: null,
    serverNonce: null,
    session: null,
    cancelled: false,
    pending: false,
    completionPending: false,
    completionFlowNonce: null,
    idempotencyKey: null,
    printerUuid: null,
  };

  if (!OPERATIONS.has(operation) || !action || allowedOrigins.length === 0 || window.parent === window) {
    fail("This route requires its configured host application.");
    return;
  }

  window.addEventListener("message", receiveParentMessage);
  ownerForm.addEventListener("submit", authorizeSession);
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
    if (state.cancelled) {
      return;
    }
    if (state.parentOrigin !== null && event.origin !== state.parentOrigin) {
      return;
    }
    if (state.completionPending) {
      receiveCompletionResult(event.data);
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

  function receiveCompletionResult(message) {
    if (
      !matches(message, ["version", "type", "flow_nonce", "status"]) ||
      message.version !== VERSION ||
      message.type !== "klove.frame.completion.result" ||
      message.flow_nonce !== state.completionFlowNonce ||
      (message.status !== "created" && message.status !== "failed")
    ) {
      return;
    }
    const created = message.status === "created";
    discardSessionMaterial({ hideLifecycle: true });
    state.cancelled = true;
    guidance.textContent = created
      ? "The host application confirmed the printer entry."
      : "The host application did not create a printer entry.";
    status.textContent = created ? "Registration handoff complete." : "Registration handoff failed.";
    cancel.disabled = true;
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
    if (credential.value.length === 0) {
      setAuthorizationError("Enter the owner credential to continue.");
      return;
    }
    try {
      preflightLifecycleIdentifiers();
    } catch (error) {
      credential.value = "";
      if (error instanceof InputError) {
        setAuthorizationError(error.message);
        return;
      }
      fail("This browser cannot safely prepare the registration action.");
      return;
    }
    const ownerCredential = credential.value;
    state.pending = true;
    setAuthorizationDisabled(true);
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
      const grant = await jsonObject(response);
      if (
        !response.ok ||
        !matches(grant, ["csrf_token", "flow_nonce", "operation", "expires_in_seconds"]) ||
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
      ownerForm.hidden = true;
      cancel.disabled = false;
      buildLifecycleForm();
      guidance.textContent = "Authorization is complete. Confirm the bounded registration details below.";
      status.textContent = "Secure owner session active.";
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

  function buildLifecycleForm() {
    lifecyclePanel.replaceChildren(lifecycleTitle, lifecycleGuidance);
    lifecyclePanel.hidden = false;
    lifecycleGuidance.textContent = action.guidance;
    lifecyclePanel.append(lifecycleGuidance);
    const form = document.createElement("form");
    form.id = "lifecycle-action";
    form.noValidate = true;
    addLifecycleFields(form);
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = action.button;
    form.append(submit);
    form.addEventListener("submit", submitLifecycle);
    lifecyclePanel.append(form);
    const first = form.querySelector("input:not([type=checkbox]), select");
    if (first instanceof HTMLElement) {
      first.focus();
    }
  }

  function addLifecycleFields(form) {
    if (operation === "create") {
      addTextField(form, "printer_uuid", "Printer UUID", {
        value: state.printerUuid,
        pattern: UUID.source,
        maxLength: 36,
        autocomplete: "off",
      });
      addTextField(form, "display_name", "Printer name", { maxLength: 100, autocomplete: "off" });
      addEndpointFields(form);
      addCredentialField(form, "moonraker_credential", "Moonraker credential");
      addProfileAndOptInFields(form);
      return;
    }
    addTextField(form, "printer_uuid", "Registered printer UUID", {
      pattern: UUID.source,
      maxLength: 36,
      autocomplete: "off",
    });
    addTextField(form, "expected_revision", "Current registration revision", {
      inputMode: "numeric",
      pattern: "[1-9][0-9]*",
      maxLength: 16,
      autocomplete: "off",
    });
    if (operation === "update") {
      addTextField(form, "display_name", "Printer name", { maxLength: 100, autocomplete: "off" });
      addEndpointFields(form);
      addProfileAndOptInFields(form);
      addCheckbox(form, "reactivate", "Reactivate this disabled printer after the direct probe", false);
    } else if (operation === "rotate_moonraker") {
      addCredentialField(form, "moonraker_credential", "Replacement Moonraker credential");
    } else if (operation === "rotate_compatibility") {
      addConfirmation(
        form,
        "confirm_rotate_compatibility",
        "I understand this invalidates the existing compatibility access for this printer.",
      );
    } else if (operation === "disable") {
      addConfirmation(form, "confirm_disable", "I understand this disables Klove control and dispatch for this printer.");
    } else if (operation === "remove") {
      addConfirmation(form, "confirm_remove", "I understand this removes only an already disabled Klove registration.");
    }
  }

  function addEndpointFields(form) {
    const fieldset = addFieldset(form, "Moonraker address");
    const hint = document.createElement("p");
    hint.className = "field-hint";
    hint.textContent = "Use an advisory discovery result or enter a manual exact HTTP(S) origin. Klove does not trust the hint and verifies the endpoint directly.";
    fieldset.append(hint);
    addTextField(fieldset, "endpoint_url", "Moonraker origin", {
      inputMode: "url",
      maxLength: 2048,
      autocomplete: "off",
      placeholder: "https://printer.example.test:7125",
    });
    addCheckbox(fieldset, "allow_insecure_http", "I explicitly accept non-loopback HTTP risk", false);
    addCheckbox(fieldset, "verify_tls", "Verify the HTTPS certificate", true);
  }

  function addCredentialField(form, name, label) {
    addTextField(form, name, label, {
      type: "password",
      inputMode: "text",
      maxLength: 4096,
      autocomplete: "off",
      spellcheck: false,
    });
  }

  function addProfileAndOptInFields(form) {
    const fieldset = addFieldset(form, "Safety-profile binding and opt-in");
    const enabled = addCheckbox(fieldset, "bind_profile", "Bind one exact Klipper safety profile", false);
    const details = document.createElement("div");
    details.className = "profile-details";
    details.hidden = true;
    addTextField(details, "profile_generation", "Safety-profile generation", {
      inputMode: "numeric",
      pattern: "[1-9][0-9]*",
      maxLength: 16,
      autocomplete: "off",
    });
    addTextField(details, "slicer_profile_id", "Registered slicer profile ID", {
      maxLength: 100,
      autocomplete: "off",
    });
    addTextField(details, "nozzle_micrometres", "Nozzle diameter (micrometres)", {
      inputMode: "numeric",
      pattern: "[1-9][0-9]*",
      maxLength: 4,
      autocomplete: "off",
    });
    addTextField(details, "build_x_micrometres", "Build width (micrometres)", {
      inputMode: "numeric",
      pattern: "[1-9][0-9]*",
      maxLength: 7,
      autocomplete: "off",
    });
    addTextField(details, "build_y_micrometres", "Build depth (micrometres)", {
      inputMode: "numeric",
      pattern: "[1-9][0-9]*",
      maxLength: 7,
      autocomplete: "off",
    });
    addTextField(details, "build_z_micrometres", "Build height (micrometres)", {
      inputMode: "numeric",
      pattern: "[1-9][0-9]*",
      maxLength: 7,
      autocomplete: "off",
    });
    addTextField(details, "build_plate_id", "Build plate ID", {
      maxLength: 100,
      autocomplete: "off",
    });
    fieldset.append(details);
    const control = addCheckbox(fieldset, "control_enabled", "Opt in to Klove job controls after positive direct capability evidence", false);
    const dispatch = addCheckbox(fieldset, "dispatch_enabled", "Opt in to Klove dispatch after positive direct capability evidence", false);
    enabled.addEventListener("change", () => {
      details.hidden = !enabled.checked;
    });
    dispatch.addEventListener("change", () => {
      if (dispatch.checked && !enabled.checked) {
        enabled.checked = true;
        details.hidden = false;
      }
    });
    control.addEventListener("change", () => {
      if (!control.checked) {
        return;
      }
      status.textContent = "Klove will permit this opt-in only with current positive capability evidence.";
    });
  }

  function addConfirmation(form, name, label) {
    const confirmation = addCheckbox(form, name, label, false);
    confirmation.required = true;
  }

  function addFieldset(parent, legendText) {
    const fieldset = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = legendText;
    fieldset.append(legend);
    parent.append(fieldset);
    return fieldset;
  }

  function addTextField(parent, name, labelText, options = {}) {
    const wrapper = document.createElement("div");
    wrapper.className = "field";
    const label = document.createElement("label");
    const id = `lifecycle-${name}`;
    label.htmlFor = id;
    label.textContent = labelText;
    const input = document.createElement("input");
    input.id = id;
    input.name = name;
    input.type = options.type || "text";
    input.required = options.required !== false;
    if (options.value) {
      input.value = options.value;
    }
    if (options.maxLength) {
      input.maxLength = options.maxLength;
    }
    if (options.pattern) {
      input.pattern = options.pattern;
    }
    if (options.inputMode) {
      input.inputMode = options.inputMode;
    }
    if (options.autocomplete) {
      input.autocomplete = options.autocomplete;
    }
    if (options.placeholder) {
      input.placeholder = options.placeholder;
    }
    if (options.spellcheck === false) {
      input.spellcheck = false;
    }
    wrapper.append(label, input);
    parent.append(wrapper);
    return input;
  }

  function addCheckbox(parent, name, labelText, checked) {
    const wrapper = document.createElement("label");
    wrapper.className = "checkbox-field";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = name;
    input.checked = checked;
    const text = document.createElement("span");
    text.textContent = labelText;
    wrapper.append(input, text);
    parent.append(wrapper);
    return input;
  }

  async function submitLifecycle(event) {
    event.preventDefault();
    if (state.pending || state.cancelled || state.session === null) {
      return;
    }
    const form = event.currentTarget;
    if (!(form instanceof HTMLFormElement)) {
      fail("The secure registration form could not be confirmed.");
      return;
    }
    try {
      const request = lifecycleRequest(form);
      state.pending = true;
      setLifecycleDisabled(true);
      const responsePromise = fetch(request.path, {
        method: request.method,
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-Klove-CSRF": state.session.csrfToken,
          "Idempotency-Key": request.idempotencyKey,
        },
        body: JSON.stringify(request.body),
      });
      clearMoonrakerCredential(form);
      const response = await responsePromise;
      const document = await jsonObject(response);
      if (response.ok && matches(document, ["printer"]) && isObject(document.printer)) {
        if (operation === "create") {
          await deliverCompletion();
          return;
        }
        completeLifecycle(document.printer);
        return;
      }
      if (!response.ok && matches(document, ["error"]) && typeof document.error === "string") {
        if (RETRYABLE_ERRORS.has(document.error)) {
          lifecycleGuidance.textContent = retryGuidance(document.error);
          status.textContent = "No registration change was confirmed.";
          setLifecycleDisabled(false);
          return;
        }
      }
      throw new Error("lifecycle operation denied");
    } catch (error) {
      if (error instanceof InputError) {
        clearMoonrakerCredential(form);
        lifecycleGuidance.textContent = error.message;
        status.textContent = "Review the bounded registration details and try again.";
        setLifecycleDisabled(false);
      } else {
        state.session = null;
        fail("Klove could not confirm the registration outcome. Restart the secure host connection before trying again.");
      }
    } finally {
      state.pending = false;
    }
  }

  function lifecycleRequest(form) {
    const printerUuid = requiredUuid(form, "printer_uuid");
    const body = { flow_nonce: state.session.flowNonce };
    if (operation === "create") {
      Object.assign(body, {
        printer_uuid: printerUuid,
        display_name: requiredText(form, "display_name", 1, 100),
        endpoint: endpoint(form),
        moonraker_credential: requiredCredential(form, "moonraker_credential"),
        safety_profiles: safetyProfiles(form, printerUuid),
        control_enabled: checked(form, "control_enabled"),
        dispatch_enabled: checked(form, "dispatch_enabled"),
      });
    } else {
      const expectedRevision = requiredInteger(form, "expected_revision");
      Object.assign(body, { expected_revision: expectedRevision });
      if (operation === "update") {
        Object.assign(body, {
          display_name: requiredText(form, "display_name", 1, 100),
          endpoint: endpoint(form),
          safety_profiles: safetyProfiles(form, printerUuid),
          control_enabled: checked(form, "control_enabled"),
          dispatch_enabled: checked(form, "dispatch_enabled"),
          reactivate: checked(form, "reactivate"),
        });
      } else if (operation === "rotate_moonraker") {
        body.moonraker_credential = requiredCredential(form, "moonraker_credential");
      } else if (operation === "rotate_compatibility") {
        requireChecked(form, "confirm_rotate_compatibility");
      } else if (operation === "disable") {
        requireChecked(form, "confirm_disable");
      } else if (operation === "remove") {
        requireChecked(form, "confirm_remove");
      }
    }
    if (state.idempotencyKey === null) {
      throw new InputError("This browser could not prepare a secure idempotency key.");
    }
    return {
      method: action.method,
      path: action.path(printerUuid),
      body,
      idempotencyKey: state.idempotencyKey,
    };
  }

  function endpoint(form) {
    const url = requiredText(form, "endpoint_url", 1, 2048);
    const scheme = canonicalEndpointScheme(url);
    const isHttp = scheme === "http";
    const isHttps = scheme === "https";
    const allowInsecureHttp = checked(form, "allow_insecure_http");
    const verifyTls = checked(form, "verify_tls");
    if ((isHttp && verifyTls) || (isHttps && allowInsecureHttp)) {
      throw new InputError("Confirm transport choices that match the exact endpoint scheme.");
    }
    return {
      url,
      allow_insecure_http: allowInsecureHttp,
      verify_tls: verifyTls,
    };
  }

  function canonicalEndpointScheme(value) {
    if (!/^[\x21-\x7e]+$/.test(value)) {
      throw new InputError("Enter one exact canonical HTTP(S) Moonraker origin.");
    }
    try {
      const parsed = new URL(value);
      if (
        (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
        parsed.origin !== value ||
        !parsed.hostname ||
        parsed.hostname.endsWith(".") ||
        parsed.hostname.includes("%")
      ) {
        throw new InputError("Enter one exact canonical HTTP(S) Moonraker origin.");
      }
      return parsed.protocol.slice(0, -1);
    } catch (error) {
      if (error instanceof InputError) {
        throw error;
      }
      throw new InputError("Enter one exact canonical HTTP(S) Moonraker origin.");
    }
  }

  function safetyProfiles(form, printerUuid) {
    if (!checked(form, "bind_profile")) {
      if (checked(form, "dispatch_enabled")) {
        throw new InputError("Dispatch opt-in requires one exact safety-profile binding.");
      }
      return [];
    }
    return [{
      printer_uuid: printerUuid,
      generation: requiredInteger(form, "profile_generation"),
      slicer_profile_id: requiredText(form, "slicer_profile_id", 1, 100),
      compatibility: {
        gcode_flavor: "klipper",
        nozzle_diameter_micrometres: requiredInteger(form, "nozzle_micrometres"),
        build_volume: {
          x_micrometres: requiredInteger(form, "build_x_micrometres"),
          y_micrometres: requiredInteger(form, "build_y_micrometres"),
          z_micrometres: requiredInteger(form, "build_z_micrometres"),
        },
        build_plate_id: requiredText(form, "build_plate_id", 1, 100),
      },
    }];
  }

  function requiredUuid(form, name) {
    const value = valueOf(form, name);
    if (!UUID.test(value)) {
      throw new InputError("Enter one canonical version 4 printer UUID.");
    }
    return value;
  }

  function requiredInteger(form, name) {
    const value = valueOf(form, name);
    if (!INTEGER.test(value)) {
      throw new InputError("Enter one positive whole-number registration value.");
    }
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number > MAX_SAFE_LIFECYCLE_INTEGER) {
      throw new InputError("Enter one safely representable registration value.");
    }
    return number;
  }

  function requiredText(form, name, minimum, maximum) {
    const value = valueOf(form, name);
    if (
      value.length < minimum ||
      value.length > maximum ||
      value !== value.trim() ||
      /[\u0000-\u001f\u007f]/.test(value)
    ) {
      throw new InputError("Enter exact bounded text without surrounding whitespace or control characters.");
    }
    return value;
  }

  function requiredCredential(form, name) {
    const value = valueOf(form, name);
    if (!VISIBLE_SECRET.test(value)) {
      throw new InputError("Enter the complete visible-ASCII Moonraker credential.");
    }
    return value;
  }

  function valueOf(form, name) {
    const input = form.elements.namedItem(name);
    if (!(input instanceof HTMLInputElement)) {
      throw new InputError("The secure registration form is incomplete.");
    }
    return input.value;
  }

  function checked(form, name) {
    const input = form.elements.namedItem(name);
    return input instanceof HTMLInputElement && input.checked;
  }

  function requireChecked(form, name) {
    if (!checked(form, name)) {
      throw new InputError("Confirm this irreversible registration action before continuing.");
    }
  }

  function completeLifecycle(printer) {
    discardSessionMaterial({ hideLifecycle: false });
    state.cancelled = true;
    lifecycleGuidance.textContent = "The owner session is closed. Start a new secure connection for another bounded action.";
    const summary = document.createElement("p");
    summary.textContent = "Klove confirmed the bounded registration operation after its canonical lifecycle checks.";
    const list = document.createElement("dl");
    appendSummary(list, "Printer UUID", publicText(printer.printer_uuid));
    appendSummary(list, "Registration revision", publicText(printer.revision));
    appendSummary(list, "Lifecycle", publicText(printer.lifecycle));
    if (isObject(printer.identity)) {
      appendSummary(list, "Direct probe host", publicText(printer.identity.klipper_hostname));
    }
    lifecyclePanel.append(lifecycleTitle, lifecycleGuidance, summary, list);
    status.textContent = "Registration operation confirmed.";
    cancel.disabled = true;
  }

  async function deliverCompletion() {
    if (
      state.parentOrigin === null ||
      state.session === null ||
      operation !== "create" ||
      state.cancelled ||
      state.completionPending
    ) {
      fail("Klove could not prepare the registration handoff. Restart the secure host connection.");
      return;
    }
    const session = state.session;
    try {
      const response = await fetch("/v1/onboarding/frame/completion", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-Klove-CSRF": session.csrfToken,
        },
        body: JSON.stringify({ flow_nonce: session.flowNonce }),
      });
      const completion = await jsonObject(response);
      if (!response.ok || !isExactCompletion(completion, session.flowNonce)) {
        throw new Error("completion denied");
      }
      state.session = null;
      state.serverNonce = null;
      state.idempotencyKey = null;
      state.printerUuid = null;
      state.completionPending = true;
      state.completionFlowNonce = completion.flow_nonce;
      lifecyclePanel.replaceChildren(lifecycleTitle, lifecycleGuidance);
      lifecyclePanel.hidden = true;
      cancel.disabled = true;
      guidance.textContent = "Klove is handing the printer details to the host application.";
      status.textContent = "Waiting for host confirmation.";
      const message = {
        version: VERSION,
        type: "klove.frame.completion",
        flow_nonce: completion.flow_nonce,
        name: completion.name,
        serial_number: completion.serial_number,
        ip_address: completion.ip_address,
        access_code: completion.access_code,
      };
      try {
        window.parent.postMessage(message, state.parentOrigin);
      } finally {
        message.access_code = "";
        completion.access_code = "";
      }
    } catch {
      state.session = null;
      fail("Klove could not deliver the registration handoff. Restart the secure host connection.");
    }
  }

  function appendSummary(list, labelText, valueText) {
    const term = document.createElement("dt");
    term.textContent = labelText;
    const detail = document.createElement("dd");
    detail.textContent = valueText;
    list.append(term, detail);
  }

  function publicText(value) {
    return typeof value === "string" || typeof value === "number" ? String(value) : "Not available";
  }

  function retryGuidance(code) {
    if (code === "probe_failed") {
      return "Klove could not establish current direct probe evidence. Retry the same exact details, or begin a new secure connection before changing them.";
    }
    if (code === "stale_revision") {
      return "Klove rejected the supplied registration revision. Start a new secure recovery connection with current exact evidence.";
    }
    return "Klove did not confirm this bounded operation. Retry the same exact details, or begin a new secure connection before changing them.";
  }

  function clearMoonrakerCredential(form) {
    const input = form.elements.namedItem("moonraker_credential");
    if (input instanceof HTMLInputElement) {
      input.value = "";
    }
  }

  async function cancelSession() {
    if (state.pending || state.cancelled || state.completionPending) {
      return false;
    }
    state.pending = true;
    setAuthorizationDisabled(true);
    setLifecycleDisabled(true);
    try {
      const cancelEndpoint = state.session === null
        ? "/v1/onboarding/frame/cancel"
        : "/v1/onboarding/cancel";
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
      const response = await fetch(cancelEndpoint, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers,
        body: JSON.stringify(body),
      });
      const result = await jsonObject(response);
      if (!response.ok || !matches(result, ["status"]) || result.status !== "cancelled") {
        throw new Error("cancellation denied");
      }
      const parentNonce = state.parentNonce;
      const parentOrigin = state.parentOrigin;
      discardSessionMaterial({ hideLifecycle: true });
      state.cancelled = true;
      guidance.textContent = "The Klove session was cancelled.";
      status.textContent = "Cancelled.";
      sendToParent({
        version: VERSION,
        type: "klove.frame.cancelled",
        operation,
        parent_nonce: parentNonce,
      }, parentOrigin);
      return true;
    } catch {
      fail("Cancellation could not be confirmed. Close this frame and begin a new connection.");
      return false;
    } finally {
      state.pending = false;
    }
  }

  function sendToParent(message, targetOrigin = state.parentOrigin) {
    if (targetOrigin !== null) {
      window.parent.postMessage(message, targetOrigin);
    }
  }

  async function jsonObject(response) {
    if (response.headers.get("content-type") !== "application/json; charset=utf-8") {
      return null;
    }
    try {
      const value = await response.json();
      return isObject(value) ? value : null;
    } catch {
      return null;
    }
  }

  function matches(value, keys) {
    if (!isObject(value)) {
      return false;
    }
    const actual = Object.keys(value).sort();
    const expected = keys.slice().sort();
    return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isToken(value) {
    return typeof value === "string" && TOKEN.test(value);
  }

  function isExactCompletion(value, flowNonce) {
    return (
      matches(value, ["version", "type", "flow_nonce", "name", "serial_number", "ip_address", "access_code"]) &&
      value.version === VERSION &&
      value.type === "klove.frame.completion" &&
      value.flow_nonce === flowNonce &&
      isDisplayName(value.name) &&
      typeof value.serial_number === "string" &&
      PROXY_SERIAL.test(value.serial_number) &&
      isCompatibilityHost(value.ip_address) &&
      typeof value.access_code === "string" &&
      ACCESS_CODE.test(value.access_code)
    );
  }

  function isDisplayName(value) {
    return (
      typeof value === "string" &&
      value.length >= 1 &&
      value.length <= 100 &&
      value === value.trim() &&
      !/[\u0000-\u001f\u007f]/.test(value)
    );
  }

  function isCompatibilityHost(value) {
    if (typeof value !== "string" || value.length === 0 || value.length > 253 || !/^[\x21-\x7e]+$/.test(value)) {
      return false;
    }
    const parts = value.split(".");
    if (parts.every((part) => /^[0-9]+$/.test(part))) {
      if (parts.length !== 4) {
        return false;
      }
      return parts.every((part) => {
        if (!/^(0|[1-9][0-9]{0,2})$/.test(part)) {
          return false;
        }
        const number = Number(part);
        return Number.isInteger(number) && number >= 0 && number <= 255 && String(number) === part;
      });
    }
    return DNS_HOST.test(value);
  }

  function secureRandomUuid() {
    if (typeof crypto.randomUUID !== "function") {
      throw new InputError("This browser cannot create a secure registration identifier.");
    }
    const value = crypto.randomUUID();
    if (!UUID.test(value)) {
      throw new InputError("This browser cannot create a secure registration identifier.");
    }
    return value;
  }

  function preflightLifecycleIdentifiers() {
    if (state.idempotencyKey !== null) {
      return;
    }
    const idempotencyKey = secureRandomUuid();
    const printerUuid = operation === "create" ? secureRandomUuid() : null;
    state.idempotencyKey = idempotencyKey;
    state.printerUuid = printerUuid;
  }

  function discardSessionMaterial({ hideLifecycle }) {
    credential.value = "";
    state.session = null;
    state.serverNonce = null;
    state.parentNonce = null;
    state.parentOrigin = null;
    state.completionPending = false;
    state.completionFlowNonce = null;
    state.idempotencyKey = null;
    state.printerUuid = null;
    lifecyclePanel.replaceChildren(lifecycleTitle, lifecycleGuidance);
    lifecyclePanel.hidden = hideLifecycle;
  }

  function setAuthorizationDisabled(disabled) {
    credential.disabled = disabled;
    authorize.disabled = disabled;
    cancel.disabled = disabled;
  }

  function setLifecycleDisabled(disabled) {
    const form = lifecyclePanel.querySelector("form");
    if (form instanceof HTMLFormElement) {
      for (const control of form.elements) {
        if (control instanceof HTMLInputElement || control instanceof HTMLButtonElement) {
          control.disabled = disabled;
        }
      }
    }
    cancel.disabled = disabled;
  }

  function setAuthorizationError(message) {
    guidance.textContent = message;
    status.textContent = "Owner authorization is required.";
  }

  function fail(message) {
    discardSessionMaterial({ hideLifecycle: true });
    state.cancelled = true;
    setAuthorizationDisabled(true);
    setLifecycleDisabled(true);
    guidance.textContent = message;
    status.textContent = "Connection denied.";
  }

  class InputError extends Error {}
})();
