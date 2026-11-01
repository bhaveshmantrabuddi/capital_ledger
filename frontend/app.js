/* Capital Ledger: dependency-free browser client. Financial state comes only
   from API responses; browser arithmetic is used only to parse/format cents. */
"use strict";

(() => {
  const config = Object.assign({
    apiBaseUrl: "", authMode: "local", cognitoDomain: "", cognitoClientId: "",
    redirectUri: "", logoutUri: "", oauthScopes: ["openid", "email", "profile"]
  }, window.APP_CONFIG || {});
  const $ = (id) => document.getElementById(id);
  const SESSION_KEY = "capital-ledger.session.v1";
  const PKCE_KEY = "capital-ledger.pkce.v1";
  const state = { health: null, token: "", userId: "", userName: "", localUser: "alice",
    accounts: [], selectedId: "", action: "deposit", busy: false, refreshing: false,
    workerPaused: false, pending: null, refreshGeneration: 0 };
  const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

  class ApiError extends Error {
    constructor(message, status = 0, code = "", uncertain = false) {
      super(message); this.name = "ApiError"; this.status = status;
      this.code = code; this.uncertain = uncertain;
    }
  }

  function showMessage(message, error = false) {
    $("message").textContent = message;
    $("message").className = error ? "notice error" : "notice";
    $("message").hidden = !message;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 20000);
    const headers = { Accept: "application/json", ...options.headers };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(config.apiBaseUrl.replace(/\/$/, "") + path, {
        method: options.method || "GET", headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal, cache: "no-store", credentials: "omit"
      });
      const raw = await response.text();
      let data = {};
      if (raw) {
        try { data = JSON.parse(raw); }
        catch { throw new ApiError("The server returned an unreadable response. Refresh or retry the saved request.", response.status, "INVALID_RESPONSE", true); }
      }
      if (!response.ok) {
        const error = data.error || {};
        throw new ApiError(error.message || `Request failed (${response.status}).`, response.status,
          error.code || "REQUEST_FAILED", response.status >= 500 || response.status === 408 || response.status === 429);
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === "AbortError"
        ? "The request timed out. Its outcome is unconfirmed."
        : "The server could not be reached. Check your connection and try again.", 0, "NETWORK_ERROR", true);
    } finally { window.clearTimeout(timer); }
  }

  function readSession() {
    try { return JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); }
    catch { return null; }
  }

  function saveSession(session) {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
    state.token = session.access_token;
    state.userName = session.name || "Signed in";
    state.localUser = session.localUser || "alice";
  }

  function pendingStorageKey() { return `capital-ledger.pending.v1.${state.userId}`; }

  function restorePending() {
    try {
      const pending = JSON.parse(sessionStorage.getItem(pendingStorageKey()) || "null");
      state.pending = pending && pending.userId === state.userId && typeof pending.key === "string"
        && typeof pending.path === "string" && pending.path.startsWith("/api/")
        && pending.body && typeof pending.body === "object" ? pending : null;
    } catch { state.pending = null; }
    renderControls();
  }

  function persistPending(pending) {
    // Persistence must succeed before a write is sent. Reloading keeps the key.
    sessionStorage.setItem(pendingStorageKey(), JSON.stringify(pending));
    state.pending = pending;
  }

  function clearPending() {
    sessionStorage.removeItem(pendingStorageKey()); state.pending = null;
  }

  function parseCents(value, allowZero = false) {
    const cleaned = String(value).trim();
    if (!/^\d+(?:\.\d{1,2})?$/.test(cleaned)) throw new Error("Enter an amount such as 25 or 25.50, with up to two decimal places.");
    const [whole, fraction = ""] = cleaned.split(".");
    const exactCents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
    if (exactCents > BigInt(Number.MAX_SAFE_INTEGER) || (!allowZero && exactCents === 0n)) {
      throw new Error(allowZero ? "Enter a valid nonnegative amount." : "Enter an amount greater than zero.");
    }
    return Number(exactCents);
  }

  function selectedAccount() { return state.accounts.find((account) => account.id === state.selectedId); }
  function formatMoney(cents) { return Number.isSafeInteger(cents) ? money.format(cents / 100) : "—"; }
  function isLocal() { return state.health?.mode === "local"; }
  function authMode() { return state.health?.auth_mode || config.authMode || "local"; }

  function renderIdentity() {
    $("user-label").textContent = state.token ? state.userName : "Not signed in";
    $("user-avatar").textContent = state.token ? state.userName.slice(0, 1).toUpperCase() : "–";
    $("auth-button").hidden = authMode() === "local";
    $("auth-button").textContent = state.token ? "Sign out" : "Sign in";
    $("demo-user").hidden = authMode() !== "local";
    $("demo-user").value = state.localUser;
    $("demo-user").disabled = state.busy || state.refreshing;
    $("auth-button").disabled = state.busy;
    $("signed-out").hidden = Boolean(state.token);
    $("welcome-signin").textContent = authMode() === "local" ? "Enter as Alice" : "Sign in";
    $("dashboard").hidden = !state.token;
    $("local-controls").hidden = !isLocal();
  }

  function renderControls() {
    const unresolved = Boolean(state.pending);
    const blocked = state.busy || unresolved || !state.token;
    for (const control of document.querySelectorAll("[data-mutation-control]")) control.disabled = blocked;
    $("money-fields").disabled = blocked || !selectedAccount();
    $("account-fields").disabled = blocked;
    $("pending-notice").hidden = !unresolved;
    $("pending-description").textContent = unresolved
      ? `${state.pending.label} · Saved ${new Date(state.pending.createdAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}. Retry with the original key to confirm the result.`
      : "";
    $("retry-request").disabled = state.busy || !state.token;
    $("retry-request").textContent = state.busy ? "Confirming…" : "Retry saved request";
    $("demo-user").disabled = state.busy || state.refreshing;
    $("auth-button").disabled = state.busy;
    $("refresh").disabled = state.refreshing || state.busy;
    $("toggle-worker").disabled = state.busy || !state.token;
    $("sync-now").disabled = state.busy || !state.token || state.workerPaused;
    $("toggle-worker").textContent = state.workerPaused ? "Resume summary sync" : "Pause summary sync";
    $("toggle-worker").setAttribute("aria-pressed", String(state.workerPaused));
    $("close-account-dialog").disabled = state.busy;
  }

  function renderAccounts() {
    const list = $("account-tabs"); list.replaceChildren();
    $("account-count").textContent = `${state.accounts.length} ${state.accounts.length === 1 ? "account" : "accounts"}`;
    for (const account of state.accounts) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = account.id === state.selectedId ? "account-tab active" : "account-tab";
      button.setAttribute("aria-pressed", String(account.id === state.selectedId));
      button.textContent = account.name;
      button.addEventListener("click", () => selectAccount(account.id));
      list.append(button);
    }
    const account = selectedAccount();
    $("account-empty").hidden = Boolean(account);
    $("balance-card").hidden = !account;
    $("account-details").hidden = !account;
    if (account) {
      $("account-name").textContent = account.name;
      $("account-balance").textContent = formatMoney(account.balance_cents);
      $("account-holder").textContent = account.full_name || state.userName;
      $("account-id").textContent = account.id;
      const synced = account.summary_synced === true;
      $("projection-dot").className = `status-dot ${synced ? "ok" : "warn"}`;
      const summaryName = isLocal() ? "Local summary" : "DynamoDB summary";
      $("projection-label").textContent = synced ? `${summaryName} is up to date` : `${summaryName} is catching up`;
      $("projection-version").textContent = `LEDGER v${account.version ?? "—"} / SUMMARY v${account.summary_version ?? "—"}`;
      $("projection-detail").textContent = synced
        ? "The displayed balance comes from the authoritative ledger."
        : `Ledger balance is current. The last summary is ${formatMoney(account.summary_balance_cents)}; pending events will bring it up to date.`;
    }
    if (authMode() === "cognito") renderCloudIntegrity();
    renderControls();
  }

  function textElement(tag, content, className = "") {
    const element = document.createElement(tag);
    element.textContent = content;
    if (className) element.className = className;
    return element;
  }

  function renderTransactions(transactions) {
    const body = $("transactions"); body.replaceChildren();
    $("transactions-empty").hidden = transactions.length > 0;
    const labels = { opening: "Opening balance", opening_balance: "Opening balance", deposit: "Deposit",
      withdrawal: "Withdrawal", transfer: "Transfer", reversal: "Reversal" };
    for (const transaction of transactions) {
      const outgoing = transaction.source_account_id === state.selectedId;
      const kind = String(transaction.kind).toLowerCase();
      const positive = kind === "withdrawal" ? false : kind === "transfer" ? !outgoing : !outgoing || kind === "deposit" || kind.startsWith("opening");
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const name = textElement("div", "", "transaction-name");
      const icon = textElement("span", kind === "transfer" ? "↔" : positive ? "↓" : "↑", "transaction-icon");
      icon.setAttribute("aria-hidden", "true");
      const description = document.createElement("div");
      description.append(textElement("strong", labels[kind] || "Transaction"));
      const detail = transaction.description || (kind === "transfer" ? (outgoing ? "To another account" : "From another account") : "Simulated funds");
      const detailElement = textElement("small", detail); detailElement.title = detail;
      description.append(detailElement); name.append(icon, description); nameCell.append(name);
      const dateCell = textElement("td", "", "transaction-date");
      const date = new Date(transaction.created_at);
      dateCell.append(textElement("span", Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })));
      dateCell.append(textElement("small", Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })));
      const reference = textElement("td", transaction.id?.slice(0, 12) || "—", "transaction-reference");
      reference.title = transaction.id || "";
      const amount = textElement("td", `${positive ? "+" : "−"}${formatMoney(Math.abs(transaction.amount_cents))}`, `transaction-amount align-right${positive ? " positive" : ""}`);
      row.append(nameCell, dateCell, reference, amount); body.append(row);
    }
  }

  function renderReconciliation(report) {
    if (typeof report.worker_paused === "boolean") state.workerPaused = report.worker_paused;
    const mismatches = report.mismatches || [];
    const pending = report.pending_events || 0;
    const ok = report.ok === true;
    $("reconciliation-result").textContent = ok ? "Balances verified" : "Review needed";
    const checked = report.accounts_checked ?? 0;
    $("reconciliation-detail").textContent = `${checked} ${checked === 1 ? "account" : "accounts"} checked against their ledger entries.${mismatches.length ? ` ${mismatches.length} discrepancies found.` : " No discrepancies found."}`;
    $("pending-count").textContent = `${pending} pending ${pending === 1 ? "event" : "events"}`;
    $("outbox-detail").textContent = state.workerPaused
      ? "Summary sync is paused. Ledger transactions remain available."
      : pending ? `Oldest pending event: ${Math.max(0, Math.round(report.oldest_pending_seconds || 0))}s. The worker will apply committed updates.`
        : "All committed updates have reached the account summaries.";
    $("integrity-badge").textContent = !ok ? "Review findings" : pending ? "Summary sync pending" : "All checks passed";
    $("integrity-badge").className = `integrity-badge${!ok ? " error" : pending ? " warn" : ""}`;
    $("mismatch-details").hidden = !mismatches.length;
    $("mismatch-json").textContent = mismatches.length ? JSON.stringify(mismatches, null, 2) : "";
  }

  function renderCloudIntegrity() {
    const pending = state.accounts.filter(account => account.summary_synced !== true).length;
    $("reconciliation-result").textContent = "Protected check";
    $("reconciliation-detail").textContent = "Full ledger reconciliation is run by backend operations and is not exposed through the public API.";
    $("pending-count").textContent = state.accounts.length
      ? `${pending} displayed ${pending === 1 ? "summary" : "summaries"} awaiting sync` : "No accounts yet";
    $("outbox-detail").textContent = !state.accounts.length
      ? "Create an account to see its customer summary status."
      : pending ? "Some displayed customer summaries have not yet matched their authoritative account balances."
        : "The displayed customer summaries match their authoritative account balances and versions.";
    $("integrity-badge").textContent = !state.accounts.length ? "No account summaries"
      : pending ? "Summary sync pending" : "Account summaries synced";
    $("integrity-badge").className = `integrity-badge${pending ? " warn" : ""}`;
    $("mismatch-details").hidden = true;
    $("mismatch-json").textContent = "";
  }

  function resetIntegrity() {
    if (authMode() === "cognito") { renderCloudIntegrity(); return; }
    $("reconciliation-result").textContent = "—";
    $("reconciliation-detail").textContent = "Checking account balances against their ledger entries.";
    $("pending-count").textContent = "—";
    $("outbox-detail").textContent = "Checking committed account summary updates.";
    $("integrity-badge").textContent = "Waiting for check";
    $("integrity-badge").className = "integrity-badge";
    $("mismatch-details").hidden = true;
    $("mismatch-json").textContent = "";
  }

  async function loadTransactions(generation = state.refreshGeneration) {
    const accountId = state.selectedId;
    if (!accountId) { renderTransactions([]); return; }
    const data = await api(`/api/transactions?account_id=${encodeURIComponent(accountId)}`);
    if (generation === state.refreshGeneration && accountId === state.selectedId) renderTransactions(data.transactions || []);
  }

  async function refreshDashboard({ quiet = false } = {}) {
    if (!state.token || state.refreshing) return;
    state.refreshing = true; const generation = ++state.refreshGeneration; renderControls();
    try {
      const accountData = await api("/api/accounts");
      if (generation !== state.refreshGeneration) return;
      state.accounts = accountData.accounts || [];
      if (!selectedAccount()) state.selectedId = state.accounts[0]?.id || "";
      renderAccounts();
      const checks = [loadTransactions(generation)];
      if (isLocal()) checks.push(api("/api/reconciliation"));
      const results = await Promise.allSettled(checks);
      if (generation !== state.refreshGeneration) return;
      if (results[0].status === "rejected") throw results[0].reason;
      if (!isLocal()) { renderCloudIntegrity(); return; }
      if (results[1].status === "fulfilled") renderReconciliation(results[1].value);
      else {
        $("integrity-badge").textContent = "Check unavailable";
        $("integrity-badge").className = "integrity-badge warn";
        $("reconciliation-result").textContent = "Check unavailable";
        $("reconciliation-detail").textContent = results[1].reason.message;
      }
    } catch (error) {
      if (error.status === 401) expireSession();
      if (!quiet) showMessage(error.message, true);
    } finally { state.refreshing = false; renderControls(); }
  }

  async function selectAccount(id) {
    state.selectedId = id; const generation = ++state.refreshGeneration;
    renderAccounts(); renderTransactions([]);
    try { await loadTransactions(generation); }
    catch (error) { showMessage(error.message, true); }
  }

  function setAction(action) {
    state.action = action;
    for (const button of document.querySelectorAll("[data-action]")) {
      const active = button.dataset.action === action;
      button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
    }
    $("destination-field").hidden = action !== "transfer";
    $("destination").required = action === "transfer";
    const labels = { deposit: "Add simulated funds", withdrawal: "Withdraw simulated funds", transfer: "Send transfer" };
    $("money-submit").replaceChildren(document.createTextNode(labels[action]), textElement("span", "↗"));
    $("money-submit").lastChild.setAttribute("aria-hidden", "true");
    $("money-note").textContent = action === "deposit"
      ? "Deposits create simulated funds. No bank or payment network is connected."
      : action === "withdrawal" ? "Withdrawals remove simulated funds. No real money is paid out."
        : "Transfers commit both sides together in the authoritative ledger.";
  }

  async function executeMutation(pending, retry = false) {
    if (state.busy || !state.token) return;
    if (state.pending && !retry) { showMessage("Confirm the saved operation before starting a new one.", true); return; }
    try { if (!retry) persistPending(pending); }
    catch { showMessage("Browser session storage is unavailable. Enable it before submitting a money request so retries can use the same key.", true); return; }
    state.busy = true; renderControls(); showMessage("");
    let result;
    try {
      result = await api(pending.path, { method: "POST", body: pending.body, headers: { "Idempotency-Key": pending.key } });
      clearPending();
      if (result.account?.id) state.selectedId = result.account.id;
      if (pending.path === "/api/accounts") { $("account-dialog").close(); $("account-form").reset(); }
      else $("money-form").reset();
      const reference = result.transaction_id ? ` Reference ${result.transaction_id.slice(0, 12)}.` : "";
      showMessage(`${pending.label} confirmed.${reference}${result.projection_synced === false ? " The ledger is current; the customer summary will catch up." : ""}`);
    } catch (error) {
      if (!error.uncertain) clearPending();
      if (error.status === 401) expireSession();
      showMessage(error.uncertain ? `${error.message} Use “Retry saved request” to safely confirm the original operation.` : error.message, true);
      if ($("account-dialog").open) { $("account-form-error").textContent = error.message; $("account-form-error").hidden = false; }
    } finally { state.busy = false; renderControls(); }
    if (result) await refreshDashboard({ quiet: true });
  }

  function newPending(path, body, label) {
    return { path, body, label, key: crypto.randomUUID(), userId: state.userId, createdAt: new Date().toISOString() };
  }

  async function submitMoney(event) {
    event.preventDefault();
    const account = selectedAccount();
    if (!account || state.pending || state.busy) return;
    try {
      const amount = parseCents($("amount").value);
      const description = $("description").value.trim();
      const body = { amount_cents: amount, description };
      let path;
      if (state.action === "transfer") {
        const destination = $("destination").value.trim();
        if (!destination) { $("destination").focus(); throw new Error("Enter a recipient account ID."); }
        if (destination === account.id) { $("destination").focus(); throw new Error("Choose a different destination account."); }
        path = "/api/transfers"; body.source_account_id = account.id; body.destination_account_id = destination;
      } else path = `/api/accounts/${encodeURIComponent(account.id)}/${state.action === "deposit" ? "deposits" : "withdrawals"}`;
      const labels = { deposit: "Deposit", withdrawal: "Withdrawal", transfer: "Transfer" };
      await executeMutation(newPending(path, body, `${labels[state.action]} of ${formatMoney(amount)}`));
    } catch (error) { showMessage(error.message, true); if (error.message.startsWith("Enter an amount")) $("amount").focus(); }
  }

  async function submitAccount(event) {
    event.preventDefault();
    if (state.pending || state.busy) return;
    $("account-form-error").hidden = true;
    try {
      if (!$("account-form").reportValidity()) return;
      const name = $("new-account-name").value.trim();
      const fullName = $("full-name").value.trim();
      if (!name || !fullName) throw new Error("Enter a name and an account name.");
      const body = { name, full_name: fullName, date_of_birth: $("date-of-birth").value,
        initial_balance_cents: parseCents($("opening-balance").value, true) };
      await executeMutation(newPending("/api/accounts", body, "Account creation"));
    } catch (error) { $("account-form-error").textContent = error.message; $("account-form-error").hidden = false; }
  }

  function openAccountDialog() {
    if (state.pending || state.busy || !state.token) return;
    $("account-form-error").hidden = true;
    $("full-name").value = state.userName === "Signed in" ? "" : state.userName;
    $("account-dialog").showModal(); $("full-name").focus();
  }

  async function establishIdentity() {
    const me = await api("/api/me");
    state.userId = me.user_id;
    if (!state.userId) throw new Error("The server did not return an authenticated identity.");
    restorePending(); renderIdentity();
    await refreshDashboard();
  }

  async function loginLocal(user) {
    if (state.busy) return;
    state.busy = true; renderControls(); showMessage("");
    ++state.refreshGeneration;
    try {
      // Switching users changes credentials before reading any user data.
      const data = await api("/api/dev/login", { method: "POST", body: { user } });
      if (!data.access_token) throw new Error("The server did not return an access token.");
      saveSession({ access_token: data.access_token, name: data.user?.name || (user === "alice" ? "Alice Morgan" : "Bob Rivera"), localUser: user });
      state.accounts = []; state.selectedId = ""; state.pending = null;
      renderAccounts(); renderTransactions([]); resetIntegrity();
      await establishIdentity();
    } catch (error) { showMessage(error.message, true); }
    finally { state.busy = false; renderIdentity(); renderControls(); }
  }

  function base64Url(bytes) {
    return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function cognitoOrigin() {
    if (!config.cognitoDomain || !config.cognitoClientId) throw new Error("Cognito sign-in is not configured. Set the public values in config.js.");
    const url = new URL(config.cognitoDomain);
    if (url.protocol !== "https:") throw new Error("The Cognito domain must use HTTPS.");
    return url.origin;
  }

  async function loginCognito() {
    try {
      const domain = cognitoOrigin();
      const verifier = base64Url(crypto.getRandomValues(new Uint8Array(48)));
      const stateValue = base64Url(crypto.getRandomValues(new Uint8Array(24)));
      const challenge = base64Url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
      const redirectUri = config.redirectUri || location.origin + "/";
      sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state: stateValue, redirectUri, createdAt: Date.now() }));
      const url = new URL("/oauth2/authorize", domain);
      url.search = new URLSearchParams({ response_type: "code", client_id: config.cognitoClientId,
        redirect_uri: redirectUri, scope: config.oauthScopes.join(" "), state: stateValue,
        code_challenge: challenge, code_challenge_method: "S256" }).toString();
      location.assign(url.href);
    } catch (error) { showMessage(error.message, true); }
  }

  async function finishCognitoLogin() {
    const params = new URLSearchParams(location.search);
    if (!params.has("code") && !params.has("error")) return false;
    const code = params.get("code"); const returnedState = params.get("state");
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(PKCE_KEY) || "null"); }
    catch { saved = null; }
    sessionStorage.removeItem(PKCE_KEY);
    // Drop authorization parameters before rendering or fetching application data.
    history.replaceState({}, document.title, location.pathname + location.hash);
    if (!saved || saved.state !== returnedState || Date.now() - saved.createdAt > 10 * 60 * 1000) {
      throw new Error("The sign-in response could not be verified. Please start sign-in again.");
    }
    if (params.has("error")) throw new Error(params.get("error_description") || "Sign-in was cancelled.");
    const response = await fetch(new URL("/oauth2/token", cognitoOrigin()), {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "authorization_code", client_id: config.cognitoClientId,
        code, redirect_uri: saved.redirectUri, code_verifier: saved.verifier }), credentials: "omit"
    });
    const data = await response.json();
    if (!response.ok || !data.access_token) throw new Error("Sign-in could not be completed. Please try again.");
    saveSession({ access_token: data.access_token, name: "Signed in", expiresAt: Date.now() + Number(data.expires_in || 3600) * 1000 });
    return true;
  }

  function expireSession() {
    sessionStorage.removeItem(SESSION_KEY);
    state.token = ""; state.accounts = []; state.selectedId = ""; state.pending = null;
    ++state.refreshGeneration; resetIntegrity(); renderIdentity(); renderControls();
  }

  function logout() {
    expireSession();
    try {
      const url = new URL("/logout", cognitoOrigin());
      url.search = new URLSearchParams({ client_id: config.cognitoClientId,
        logout_uri: config.logoutUri || config.redirectUri || location.origin + "/" }).toString();
      location.assign(url.href);
    } catch (error) { showMessage(error.message, true); }
  }

  async function workerAction(action) {
    if (state.busy) return;
    state.busy = true; renderControls();
    try {
      if (action === "toggle") {
        const next = !state.workerPaused;
        const data = await api("/api/dev/worker", { method: "POST", body: { paused: next } });
        state.workerPaused = typeof data.paused === "boolean" ? data.paused : next;
        showMessage(state.workerPaused ? "Summary sync paused. New transactions still commit to the ledger." : "Summary sync resumed. Committed events can now catch up.");
      } else {
        const data = await api("/api/dev/sync", { method: "POST", body: {} });
        showMessage(`Summary sync processed ${data.processed ?? 0} events.`);
      }
    } catch (error) { showMessage(error.message, true); }
    finally { state.busy = false; renderControls(); }
    await refreshDashboard({ quiet: true });
  }

  function bindEvents() {
    for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", () => setAction(button.dataset.action));
    $("money-form").addEventListener("submit", submitMoney);
    $("account-form").addEventListener("submit", submitAccount);
    $("open-account").addEventListener("click", openAccountDialog);
    $("empty-open-account").addEventListener("click", openAccountDialog);
    $("close-account-dialog").addEventListener("click", () => $("account-dialog").close());
    $("account-dialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
    $("refresh").addEventListener("click", () => refreshDashboard());
    $("retry-request").addEventListener("click", () => { if (state.pending) executeMutation(state.pending, true); });
    $("demo-user").addEventListener("change", () => loginLocal($("demo-user").value));
    $("auth-button").addEventListener("click", () => state.token ? logout() : loginCognito());
    $("welcome-signin").addEventListener("click", () => authMode() === "local" ? loginLocal("alice") : loginCognito());
    $("toggle-worker").addEventListener("click", () => workerAction("toggle"));
    $("sync-now").addEventListener("click", () => workerAction("sync"));
    $("copy-account-id").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(state.selectedId); showMessage("Account ID copied. You can use it as a transfer recipient."); }
      catch { showMessage("Copy is unavailable in this browser. Select and copy the account ID shown below the balance.", true); }
    });
    const today = new Date();
    $("date-of-birth").max = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  }

  async function start() {
    bindEvents(); renderIdentity(); renderControls();
    try {
      state.health = await api("/api/health");
      if (typeof state.health.worker_paused === "boolean") state.workerPaused = state.health.worker_paused;
      $("environment-label").textContent = isLocal() ? "Free local mode" : "AWS sandbox";
      $("environment-dot").className = "status-dot ok";
      renderIdentity();
      if (authMode() === "cognito") await finishCognitoLogin();
      const session = readSession();
      if (session?.access_token && (!session.expiresAt || session.expiresAt > Date.now() + 30000)) {
        saveSession(session); await establishIdentity();
      } else if (authMode() === "local") await loginLocal("alice");
      else { expireSession(); renderIdentity(); }
    } catch (error) {
      $("environment-label").textContent = "Connection unavailable";
      $("environment-dot").className = "status-dot error";
      if (error.status === 401) expireSession();
      showMessage(error.message, true);
    }
    renderIdentity(); renderControls();
    // Polling never creates money operations. It only refreshes visible state.
    window.setInterval(() => {
      if (state.token && !state.busy && !document.hidden && !$("account-dialog").open) refreshDashboard({ quiet: true });
    }, 8000);
  }

  start();
})();
