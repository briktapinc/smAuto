/**
 * Auth-only shell for unauthenticated Studio visits.
 * On success, full-page navigate to / so the server serves the workspace HTML.
 */
const TOKEN_KEY = "bubblepod.authToken";
const STUDIO_BASE = (typeof window !== "undefined" && window.__STUDIO_BASE__) || "";

const $ = (sel) => document.querySelector(sel);

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

function goStudio(hash = "") {
  const base = withBase("/") || "/";
  const url = hash ? `${base.replace(/\/?$/, "/")}${hash.replace(/^\//, "")}` : base;
  location.replace(url);
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
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function showView(view = "login", message = "") {
  const views = {
    login: "#login-form",
    signup: "#signup-form",
    forgot: "#forgot-form",
    reset: "#reset-form",
  };
  Object.entries(views).forEach(([name, sel]) => {
    const el = $(sel);
    if (el) el.hidden = name !== view;
  });
  const err = $("#login-error");
  if (err) {
    err.hidden = !message;
    err.textContent = message || "";
  }
  if (view === "login") $("#login-user")?.focus();
  if (view === "forgot") $("#forgot-email")?.focus();
  if (view === "reset") $("#reset-pass")?.focus();
  if (view === "signup") $("#signup-user")?.focus();
}

async function enterStudio(data) {
  setToken(data?.token || getToken() || "");
  goStudio();
}

async function bootLogin() {
  const hash = location.hash || "";
  const resetMatch = hash.match(/^#reset=([^&]+)/);
  if (resetMatch) {
    window.__resetToken = decodeURIComponent(resetMatch[1]);
    showView("reset");
    return;
  }

  // Bearer token in localStorage but no cookie → refresh cookie then load workspace.
  if (getToken()) {
    try {
      await api("/api/auth/me");
      goStudio(hash);
      return;
    } catch {
      setToken("");
    }
  }

  showView("login");
  try {
    const msg = sessionStorage.getItem("bubblepod.authMsg");
    if (msg) {
      sessionStorage.removeItem("bubblepod.authMsg");
      showView("login", msg);
    }
  } catch { /* ignore */ }
}

$("#login-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#login-error");
  const btn = $("#login-submit");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: {
        username: ($("#login-user")?.value || "").trim(),
        password: $("#login-pass")?.value || "",
      },
    });
    await enterStudio(data);
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Sign in failed";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#signup-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#signup-error");
  const btn = $("#signup-submit");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/signup", {
      method: "POST",
      body: {
        username: ($("#signup-user")?.value || "").trim(),
        email: ($("#signup-email")?.value || "").trim(),
        password: $("#signup-pass")?.value || "",
      },
    });
    await enterStudio(data);
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Sign up failed";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#forgot-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#forgot-error");
  const ok = $("#forgot-ok");
  const btn = $("#forgot-submit");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (ok) { ok.hidden = true; ok.textContent = ""; }
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/forgot-password", {
      method: "POST",
      body: {
        email: ($("#forgot-email")?.value || "").trim(),
        username: ($("#forgot-user")?.value || "").trim(),
      },
    });
    if (ok) {
      ok.hidden = false;
      ok.textContent = data.message || "If that account has an email, a reset link was sent.";
    }
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Could not send reset email";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#reset-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#reset-error");
  const btn = $("#reset-submit");
  if (err) { err.hidden = true; err.textContent = ""; }
  if (btn) btn.disabled = true;
  try {
    const data = await api("/api/auth/reset-password", {
      method: "POST",
      body: {
        token: window.__resetToken || "",
        new_password: $("#reset-pass")?.value || "",
        confirm_password: $("#reset-pass2")?.value || "",
      },
    });
    location.hash = "";
    window.__resetToken = "";
    await enterStudio(data);
  } catch (ex) {
    if (err) {
      err.hidden = false;
      err.textContent = ex.message || "Reset failed";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("#login-signup-toggle")?.addEventListener("click", () => showView("signup"));
$("#login-forgot-toggle")?.addEventListener("click", () => showView("forgot"));
$("#signup-login-toggle")?.addEventListener("click", () => showView("login"));
$("#forgot-login-toggle")?.addEventListener("click", () => showView("login"));
$("#reset-login-toggle")?.addEventListener("click", () => {
  location.hash = "";
  window.__resetToken = "";
  showView("login");
});

bootLogin();
