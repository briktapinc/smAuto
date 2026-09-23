const TOKEN_KEY = "bubblepod.authToken";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

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
  } catch {}
}

function toast(msg, bad = false) {
  const el = $("#toast");
  if (!el) return;
  el.hidden = false;
  el.classList.toggle("bad", bad);
  el.textContent = msg;
  setTimeout(() => { el.hidden = true; }, 4000);
}

function money(cents, currency = "usd") {
  const n = Number(cents || 0) / 100;
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: (currency || "usd").toUpperCase() }).format(n);
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
    throw err;
  }
  return data;
}

function showLogin() {
  $("#admin-login").hidden = false;
  $("#admin-app").hidden = true;
}

function showApp(me) {
  $("#admin-login").hidden = true;
  $("#admin-app").hidden = false;
  $("#admin-who").textContent = `${me.username} · ${me.role} · equal member access`;
}

function setPanel(name) {
  $$(".top nav [data-panel]").forEach((b) => b.classList.toggle("on", b.dataset.panel === name));
  $$(".panel").forEach((p) => p.classList.toggle("on", p.id === `panel-${name}`));
  history.replaceState(null, "", `#${name}`);
}

async function loadOverview() {
  const data = await api("/api/admin/overview");
  const cur = data.currency || "usd";
  let queueBits = [];
  try {
    const q = await api("/api/queue");
    queueBits = [
      ["Pipeline running", q.running_count ?? 0],
      ["Pipeline queued", q.queued_count ?? 0],
      ["Max concurrent", q.max_concurrent ?? 1],
    ];
  } catch {}
  const cards = [
    ["Members active", data.members_active],
    ["Members total", data.members_total],
    ["Admins", data.admins],
    ...queueBits,
    ["Signups", data.signups_count],
    ["Payments", data.payments_count],
    ["Refunds", data.refunds_count],
    ["Gross", money(data.gross_revenue_cents, cur)],
    ["Net", money(data.net_revenue_cents, cur)],
  ];
  $("#overview-cards").innerHTML = cards.map(([label, value]) => (
    `<div class="stat"><span class="muted">${label}</span><strong>${value}</strong></div>`
  )).join("");
  const st = $("#stripe-status");
  if (st) {
    st.textContent = data.stripe_configured
      ? `Stripe configured · price ${data.catalog?.price_id || "—"} · ${money(data.catalog?.amount_cents, data.catalog?.currency)} / ${data.catalog?.interval}`
      : "Stripe not configured yet — add keys on the Stripe panel.";
  }
}

function badge(ok, label) {
  return `<span class="badge ${ok ? "ok" : "bad"}">${label}</span>`;
}

async function loadMembers() {
  const data = await api("/api/admin/members");
  const rows = data.users || [];
  $("#members-table").innerHTML = `
    <table>
      <thead><tr><th>User</th><th>Role</th><th>Access</th><th>Subscription</th><th>Stripe</th><th></th></tr></thead>
      <tbody>
        ${rows.map((u) => `
          <tr>
            <td><strong>${esc(u.username)}</strong><br><span class="muted">${esc(u.email || "—")}</span></td>
            <td>${esc(u.role)}</td>
            <td>${badge(!!u.has_access, u.has_access ? "full access" : "locked")}</td>
            <td>${esc(u.subscription_status || "none")}</td>
            <td class="muted">${esc(u.stripe_customer_id || "—")}</td>
            <td>
              <button type="button" data-edit="${esc(u.id)}">Edit</button>
              <button type="button" data-del="${esc(u.id)}">Delete</button>
            </td>
          </tr>`).join("") || `<tr><td colspan="6" class="muted">No users yet.</td></tr>`}
      </tbody>
    </table>`;
  $("#members-table").onclick = async (e) => {
    const edit = e.target.closest("[data-edit]");
    const del = e.target.closest("[data-del]");
    if (edit) {
      const u = rows.find((x) => x.id === edit.dataset.edit);
      if (u) openMemberDialog(u);
      return;
    }
    if (del) {
      if (!confirm("Delete this user?")) return;
      try {
        await api(`/api/admin/members/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" });
        toast("User deleted");
        await loadMembers();
        await loadOverview();
      } catch (err) {
        toast(err.message, true);
      }
    }
  };
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function openMemberDialog(user = null) {
  $("#member-dialog-title").textContent = user ? "Edit user" : "Add user";
  $("#member-id").value = user?.id || "";
  $("#member-username").value = user?.username || "";
  $("#member-username").disabled = !!user;
  $("#member-email").value = user?.email || "";
  $("#member-password").value = "";
  $("#member-password").required = !user;
  $("#member-role").value = user?.role || "member";
  $("#member-status").value = user?.subscription_status || "none";
  $("#member-disabled").checked = !!user?.disabled;
  $("#member-form-error").hidden = true;
  $("#member-dialog").showModal();
}

async function loadLedger(kind, elId, columns) {
  const data = await api(`/api/admin/billing/${kind}`);
  const rows = data.rows || [];
  const head = columns.map((c) => `<th>${c[0]}</th>`).join("");
  const body = rows.map((row) => {
    const cells = columns.map((c) => `<td>${c[1](row)}</td>`).join("");
    return `<tr>${cells}</tr>`;
  }).join("") || `<tr><td colspan="${columns.length}" class="muted">No rows yet. Webhooks will fill this ledger.</td></tr>`;
  $(elId).innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function loadStripeForm() {
  const settings = await api("/api/settings");
  $("#stripe-pub").value = settings.stripe_publishable_key || "";
  $("#stripe-price").value = settings.stripe_price_id || "";
  $("#stripe-amount").value = settings.stripe_price_amount_cents || 2900;
  $("#stripe-public-url").value = settings.public_base_url || settings.ngrok_url || "";
  $("#stripe-membership-required").checked = settings.membership_required !== false;
  $("#stripe-secret").placeholder = settings.stripe_secret_key_set ? "******** (set — enter new to change)" : "sk_test_... or rk_...";
  $("#stripe-whsec").placeholder = settings.stripe_webhook_secret_set ? "******** (set — enter new to change)" : "whsec_...";
  const base = settings.public_base_url || settings.ngrok_url || `${location.origin}`;
  $("#stripe-webhook-url").textContent = `${String(base).replace(/\/$/, "")}/api/stripe/webhook`;
}

$("#admin-login-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#admin-login-error");
  err.hidden = true;
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: {
        username: $("#admin-user").value.trim(),
        password: $("#admin-pass").value,
      },
    });
    if (!data.is_admin) {
      throw new Error("This account is not an admin.");
    }
    setToken(data.token || "");
    showApp(data);
    await refreshAll();
  } catch (ex) {
    err.hidden = false;
    err.textContent = ex.message || "Sign-in failed";
  }
});

$("#admin-logout")?.addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch {}
  setToken("");
  // Marketing home (site root), not Studio login at /app/.
  location.replace("/");
});

$$(".top nav [data-panel]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const name = btn.dataset.panel;
    setPanel(name);
    try {
      if (name === "overview") await loadOverview();
      if (name === "members") await loadMembers();
      if (name === "signups") await loadLedger("signups", "#signups-table", [
        ["When", (r) => esc(r.recorded_at || "")],
        ["User", (r) => esc(r.username || r.user_id || "")],
        ["Email", (r) => esc(r.email || "")],
        ["Amount", (r) => money(r.amount_total, r.currency)],
        ["Session", (r) => esc(r.stripe_id || "")],
      ]);
      if (name === "payments") await loadLedger("payments", "#payments-table", [
        ["When", (r) => esc(r.recorded_at || "")],
        ["Status", (r) => badge(!!r.ok, r.event || r.status || "")],
        ["Amount", (r) => money(r.amount_paid ?? r.amount_due, r.currency)],
        ["User", (r) => esc(r.user_id || "")],
        ["Invoice", (r) => esc(r.stripe_id || "")],
        ["PI", (r) => esc(r.payment_intent || "")],
      ]);
      if (name === "refunds") await loadLedger("refunds", "#refunds-table", [
        ["When", (r) => esc(r.recorded_at || "")],
        ["Amount", (r) => money(r.amount, r.currency)],
        ["Status", (r) => esc(r.status || "")],
        ["Charge / PI", (r) => esc(r.charge_id || r.payment_intent || "")],
        ["Source", (r) => esc(r.source || "")],
      ]);
      if (name === "subscriptions") await loadLedger("subscriptions", "#subscriptions-table", [
        ["When", (r) => esc(r.recorded_at || "")],
        ["Event", (r) => esc(r.event || "")],
        ["Status", (r) => esc(r.status || "")],
        ["User", (r) => esc(r.user_id || "")],
        ["Subscription", (r) => esc(r.subscription_id || "")],
      ]);
      if (name === "stripe") await loadStripeForm();
      if (name === "email") await loadEmailForm();
    } catch (err) {
      toast(err.message, true);
    }
  });
});

$("#member-new-btn")?.addEventListener("click", () => openMemberDialog(null));

$("#member-form")?.addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const err = $("#member-form-error");
  err.hidden = true;
  const id = $("#member-id").value;
  const payload = {
    email: $("#member-email").value.trim(),
    role: $("#member-role").value,
    subscription_status: $("#member-status").value,
    disabled: $("#member-disabled").checked,
  };
  const password = $("#member-password").value;
  if (password) payload.password = password;
  try {
    if (id) {
      await api(`/api/admin/members/${encodeURIComponent(id)}`, { method: "PATCH", body: payload });
    } else {
      await api("/api/admin/members", {
        method: "POST",
        body: {
          username: $("#member-username").value.trim(),
          password,
          ...payload,
        },
      });
    }
    $("#member-dialog").close();
    toast("Saved");
    await loadMembers();
    await loadOverview();
  } catch (ex) {
    err.hidden = false;
    err.textContent = ex.message;
  }
});

$("#refund-new-btn")?.addEventListener("click", () => {
  $("#refund-form-error").hidden = true;
  $("#refund-pi").value = "";
  $("#refund-charge").value = "";
  $("#refund-amount").value = "";
  $("#refund-dialog").showModal();
});

$("#refund-form")?.addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const err = $("#refund-form-error");
  err.hidden = true;
  const amountRaw = $("#refund-amount").value;
  try {
    await api("/api/admin/billing/refunds", {
      method: "POST",
      body: {
        payment_intent: $("#refund-pi").value.trim(),
        charge_id: $("#refund-charge").value.trim(),
        amount_cents: amountRaw ? Number(amountRaw) : null,
        reason: $("#refund-reason").value,
      },
    });
    $("#refund-dialog").close();
    toast("Refund created");
    await loadLedger("refunds", "#refunds-table", [
      ["When", (r) => esc(r.recorded_at || "")],
      ["Amount", (r) => money(r.amount, r.currency)],
      ["Status", (r) => esc(r.status || "")],
      ["Charge / PI", (r) => esc(r.charge_id || r.payment_intent || "")],
      ["Source", (r) => esc(r.source || "")],
    ]);
    await loadOverview();
  } catch (ex) {
    err.hidden = false;
    err.textContent = ex.message;
  }
});

$("#stripe-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    stripe_publishable_key: $("#stripe-pub").value.trim(),
    stripe_price_id: $("#stripe-price").value.trim(),
    stripe_price_amount_cents: Number($("#stripe-amount").value || 2900),
    public_base_url: $("#stripe-public-url").value.trim(),
    membership_required: $("#stripe-membership-required").checked,
  };
  const secret = $("#stripe-secret").value.trim();
  const whsec = $("#stripe-whsec").value.trim();
  if (secret && secret !== "********") body.stripe_secret_key = secret;
  if (whsec && whsec !== "********") body.stripe_webhook_secret = whsec;
  try {
    await api("/api/settings", { method: "PUT", body });
    toast("Stripe settings saved");
    $("#stripe-secret").value = "";
    $("#stripe-whsec").value = "";
    await loadStripeForm();
    await loadOverview();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#stripe-ensure-catalog")?.addEventListener("click", async () => {
  try {
    const data = await api("/api/billing/ensure-catalog", { method: "POST", body: {} });
    toast(`Catalog ready: ${data.price_id || "ok"}`);
    await loadStripeForm();
    await loadOverview();
  } catch (err) {
    toast(err.message, true);
  }
});

let emailTemplateState = {};

async function loadEmailForm() {
  const settings = await api("/api/settings");
  $("#email-enabled").checked = !!settings.email_enabled;
  $("#smtp-host").value = settings.smtp_host || "";
  $("#smtp-port").value = settings.smtp_port || 587;
  $("#smtp-user").value = settings.smtp_user || "";
  $("#smtp-password").placeholder = settings.smtp_password_set ? "******** (set — enter new to change)" : "SMTP password";
  $("#smtp-tls").checked = settings.smtp_use_tls !== false;
  $("#smtp-ssl").checked = !!settings.smtp_use_ssl;
  $("#email-from").value = settings.email_from || "";
  $("#email-from-name").value = settings.email_from_name || "Stickman Automation";
  $("#email-reply-to").value = settings.email_reply_to || "";
  $("#email-status").textContent = settings.email_configured
    ? "Email is enabled and SMTP host is set."
    : "Email is off or incomplete — messages will be skipped until configured.";
  emailTemplateState = settings.email_templates || {};
  renderEmailTemplates();
}

function renderEmailTemplates() {
  const root = $("#email-templates");
  if (!root) return;
  const keys = Object.keys(emailTemplateState);
  if (!keys.length) {
    root.innerHTML = `<p class="muted">No templates loaded.</p>`;
    return;
  }
  root.innerHTML = keys.map((key) => {
    const t = emailTemplateState[key] || {};
    return `<details class="card" open>
      <summary><code>${esc(key)}</code></summary>
      <label>Subject
        <input data-email-subject="${esc(key)}" value="${esc(t.subject || "")}" />
      </label>
      <label>Body
        <textarea data-email-body="${esc(key)}" rows="6">${esc(t.text || "")}</textarea>
      </label>
    </details>`;
  }).join("");
}

$("#email-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    email_enabled: $("#email-enabled").checked,
    smtp_host: $("#smtp-host").value.trim(),
    smtp_port: Number($("#smtp-port").value || 587),
    smtp_user: $("#smtp-user").value.trim(),
    smtp_use_tls: $("#smtp-tls").checked,
    smtp_use_ssl: $("#smtp-ssl").checked,
    email_from: $("#email-from").value.trim(),
    email_from_name: $("#email-from-name").value.trim(),
    email_reply_to: $("#email-reply-to").value.trim(),
  };
  const pw = $("#smtp-password").value.trim();
  if (pw && pw !== "********") body.smtp_password = pw;
  try {
    await api("/api/settings", { method: "PUT", body });
    toast("Email settings saved");
    $("#smtp-password").value = "";
    await loadEmailForm();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#email-test-btn")?.addEventListener("click", async () => {
  try {
    const to = ($("#email-test-to").value || "").trim();
    const data = await api("/api/admin/email/test", { method: "POST", body: { to } });
    toast(`Test sent to ${data.to || to}`);
  } catch (err) {
    toast(err.message, true);
  }
});

$("#email-templates-save")?.addEventListener("click", async () => {
  const templates = {};
  $$("[data-email-subject]").forEach((el) => {
    const key = el.dataset.emailSubject;
    templates[key] = templates[key] || {};
    templates[key].subject = el.value;
  });
  $$("[data-email-body]").forEach((el) => {
    const key = el.dataset.emailBody;
    templates[key] = templates[key] || {};
    templates[key].text = el.value;
  });
  try {
    await api("/api/settings", { method: "PUT", body: { email_templates: templates } });
    toast("Templates saved");
    await loadEmailForm();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#email-templates-reset")?.addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "PUT", body: { email_templates: {} } });
    toast("Templates restored to defaults");
    await loadEmailForm();
  } catch (err) {
    toast(err.message, true);
  }
});

async function refreshAll() {
  const hash = (location.hash || "#overview").replace("#", "") || "overview";
  setPanel(hash);
  const btn = document.querySelector(`.top nav [data-panel="${hash}"]`);
  if (btn) btn.click();
  else {
    await loadOverview();
  }
}

async function boot() {
  try {
    const me = await api("/api/auth/me");
    if (!me.is_admin) {
      showLogin();
      $("#admin-login-error").hidden = false;
      $("#admin-login-error").textContent = "Sign in with an admin account.";
      return;
    }
    showApp(me);
    await refreshAll();
  } catch {
    showLogin();
  }
}

boot();
