const TOKEN_KEY = "bubblepod.authToken";

const $ = (sel) => document.querySelector(sel);

const STUDIO_BASE = (typeof window !== "undefined" && window.__STUDIO_BASE__) || "";
function withBase(path) {
  if (!path || typeof path !== "string") return path;
  if (!STUDIO_BASE) return path;
  if (/^(https?:|data:|blob:|mailto:)/i.test(path)) return path;
  if (path === STUDIO_BASE || path.startsWith(STUDIO_BASE + "/")) return path;
  if (path.startsWith("/")) return STUDIO_BASE + path;
  return path;
}

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* ignore */ }
}

function toast(msg, bad = false) {
  const el = $("#toast");
  if (!el) return;
  el.hidden = false;
  el.classList.toggle("bad", !!bad);
  el.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 4200);
}

function money(cents, currency = "usd") {
  const n = Number(cents || 0) / 100;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: (currency || "usd").toUpperCase(),
    }).format(n);
  } catch {
    return `$${n.toFixed(2)}`;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(withBase(path), {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const detail = data?.detail || data?.message || res.statusText || "Request failed";
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function showLogin() {
  $("#pricing-login").hidden = false;
  $("#pricing-app").hidden = true;
}

function showApp() {
  $("#pricing-login").hidden = true;
  $("#pricing-app").hidden = false;
}

function statusTone(status, hasAccess) {
  if (hasAccess && (status === "active" || status === "trialing")) return "ok";
  if (status === "past_due" || status === "unpaid" || status === "trialing") return "warn";
  if (status === "canceled" || status === "none" || status === "incomplete_expired") return "bad";
  return "warn";
}

function statusCopy(me) {
  const status = me.subscription_status || "none";
  const access = !!me.has_access;
  if (me.is_admin) {
    return {
      label: `Admin · ${status}`,
      hint: "Admins always have Studio access. Member billing is managed below for testing.",
      subscribe: false,
      resubscribe: false,
      manage: !!me.stripe_configured,
      cancel: false,
    };
  }
  if (!me.stripe_configured) {
    return {
      label: `${status} · billing offline`,
      hint: "Stripe is not configured yet. Ask an admin to connect billing.",
      subscribe: false,
      resubscribe: false,
      manage: false,
      cancel: false,
    };
  }
  if (status === "trialing") {
    return {
      label: "Trialing · full access",
      hint: "Your trial is active. Cancel anytime before it ends, or keep the plan and you’ll be billed when the trial converts.",
      subscribe: false,
      resubscribe: false,
      manage: true,
      cancel: true,
    };
  }
  if (status === "active" && access) {
    return {
      label: "Active · full access",
      hint: "Your membership is active. Use Manage billing to update payment details, or Cancel to end the subscription.",
      subscribe: false,
      resubscribe: false,
      manage: true,
      cancel: true,
    };
  }
  if (status === "past_due" || status === "unpaid") {
    return {
      label: `${status} · locked`,
      hint: "Payment needs attention. Update your card in Manage billing, or resubscribe if the plan ended.",
      subscribe: false,
      resubscribe: true,
      manage: true,
      cancel: true,
    };
  }
  if (status === "canceled" || status === "incomplete_expired" || status === "incomplete") {
    return {
      label: `${status} · locked`,
      hint: "Subscribe again to unlock create, edit, topics, and renders. Browse and delete stay available.",
      subscribe: false,
      resubscribe: true,
      manage: true,
      cancel: false,
    };
  }
  // none / unknown / locked
  return {
    label: `${status || "none"} · ${access ? "access" : "locked"}`,
    hint: access
      ? "You’re signed in. Manage billing anytime from this page."
      : "Subscribe to unlock Studio. Every member gets the same privileges.",
    subscribe: !access,
    resubscribe: false,
    manage: true,
    cancel: false,
  };
}

function applyPlan(me) {
  const catalog = me.catalog || {};
  const amount = catalog.amount_cents;
  const cur = catalog.currency || "usd";
  const interval = catalog.interval || "month";
  const actions = statusCopy(me);

  $("#pricing-who").textContent = `${me.username || "Member"}${me.email ? ` · ${me.email}` : ""}`;
  $("#plan-name").textContent = catalog.membership_name || "Stickman Automation Membership";
  $("#plan-price").textContent = amount != null ? money(amount, cur) : "—";
  $("#plan-interval").textContent = amount != null ? `per ${interval}` : "";
  const pill = $("#plan-status");
  pill.textContent = `Status: ${actions.label}`;
  pill.dataset.tone = statusTone(me.subscription_status, me.has_access);
  $("#plan-hint").textContent = actions.hint;

  const sub = $("#btn-subscribe");
  const re = $("#btn-resubscribe");
  const manage = $("#btn-manage");
  const cancel = $("#btn-cancel");
  sub.hidden = !actions.subscribe;
  re.hidden = !actions.resubscribe;
  manage.hidden = !actions.manage;
  cancel.hidden = !actions.cancel;
  sub.disabled = !me.stripe_configured;
  re.disabled = !me.stripe_configured;
  manage.disabled = !me.stripe_configured;
  cancel.disabled = !me.stripe_configured;

  if (me.is_admin) {
    $("#pricing-lede").textContent =
      "Admin view of the member plan. Members use this page to subscribe, resubscribe, or cancel.";
  }
}

async function refresh() {
  const me = await api("/api/billing/status");
  showApp();
  applyPlan(me);
  return me;
}

async function startCheckout() {
  const data = await api("/api/billing/checkout", { method: "POST", body: {} });
  if (data.url) {
    location.href = data.url;
    return;
  }
  if (data.portal_url) {
    location.href = data.portal_url;
    return;
  }
  throw new Error(data.reason || data.next || "Checkout unavailable");
}

async function openPortal() {
  const data = await api("/api/billing/portal", { method: "POST", body: {} });
  if (data.url) {
    location.href = data.url;
    return;
  }
  throw new Error(data.reason || "Billing portal unavailable");
}

function setBusy(busy) {
  ["btn-subscribe", "btn-resubscribe", "btn-manage", "btn-cancel"].forEach((id) => {
    const el = document.getElementById(id);
    if (el && !el.hidden) el.disabled = !!busy;
  });
}

$("#pricing-login-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = $("#pricing-login-error");
  if (err) { err.hidden = true; err.textContent = ""; }
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: {
        username: ($("#pricing-user")?.value || "").trim(),
        password: $("#pricing-pass")?.value || "",
      },
    });
    setToken(data.token || "");
    await refresh();
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Sign in failed";
    }
  }
});

$("#pricing-logout")?.addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch { /* ignore */ }
  setToken("");
  showLogin();
});

$("#btn-subscribe")?.addEventListener("click", async () => {
  const err = $("#plan-error");
  if (err) { err.hidden = true; }
  setBusy(true);
  try {
    await startCheckout();
  } catch (ex) {
    if (err) { err.hidden = false; err.textContent = ex.message; }
    toast(ex.message, true);
    setBusy(false);
  }
});

$("#btn-resubscribe")?.addEventListener("click", async () => {
  const err = $("#plan-error");
  if (err) { err.hidden = true; }
  setBusy(true);
  try {
    await startCheckout();
  } catch (ex) {
    if (err) { err.hidden = false; err.textContent = ex.message; }
    toast(ex.message, true);
    setBusy(false);
  }
});

$("#btn-manage")?.addEventListener("click", async () => {
  setBusy(true);
  try {
    await openPortal();
  } catch (ex) {
    toast(ex.message, true);
    setBusy(false);
  }
});

$("#btn-cancel")?.addEventListener("click", async () => {
  setBusy(true);
  try {
    await openPortal();
  } catch (ex) {
    toast(ex.message, true);
    setBusy(false);
  }
});

$("#pricing-signup-link")?.addEventListener("click", (ev) => {
  // Studio signup lives on the main app gate.
  ev.preventDefault();
  location.href = "/";
});

(async function boot() {
  const params = new URLSearchParams(location.search);
  if (params.get("checkout") === "success") {
    toast("Membership updated. Stripe may take a moment to sync.");
  } else if (params.get("checkout") === "canceled") {
    toast("Checkout canceled — no charge was made.");
  }
  if (!getToken()) {
    showLogin();
    return;
  }
  try {
    await refresh();
  } catch (ex) {
    if (ex.status === 401) {
      setToken("");
      showLogin();
      return;
    }
    showLogin();
    toast(ex.message || "Could not load billing", true);
  }
})();
