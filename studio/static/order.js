(function () {
  const BASE = window.__STUDIO_BASE__ || "";

  function api(path, opts) {
    const options = opts || {};
    const headers = Object.assign({}, options.headers || {});
    let body = options.body;
    if (body && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    return fetch(BASE + path, {
      method: options.method || "GET",
      headers: headers,
      body: body,
      credentials: "same-origin",
    }).then(async (res) => {
      let payload = null;
      try { payload = await res.json(); } catch (err) { payload = null; }
      if (!res.ok) {
        let detail = res.statusText;
        if (payload && payload.detail) detail = payload.detail;
        if (detail && typeof detail === "object") detail = detail.detail || detail.message || JSON.stringify(detail);
        throw new Error(detail || "Request failed");
      }
      return payload || {};
    });
  }

  function money(cents) {
    return (Number(cents || 0) / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
  }

  function showNav() {
    const link = document.getElementById("production-nav");
    if (!link) return;
    api("/api/auth/me").then((me) => {
      if (me && (me.is_admin || me.desktop_mode)) link.hidden = false;
    }).catch(() => {});
  }

  const page = document.body.dataset.page || "";
  if (page === "order") initOrder();
  else if (page === "success") initSuccess();
  else if (page === "my-orders") initMine();
  else if (page === "production") initProduction();
  showNav();
  setupOrdersMenu();

  function setupOrdersMenu() {
    document.querySelectorAll("details.nav-menu").forEach((menu) => {
      const summary = menu.querySelector("summary");
      const panel = menu.querySelector(".nav-submenu");
      if (!summary || !panel) return;

      function visibleLinks() {
        return Array.from(panel.querySelectorAll("a[href]")).filter((link) => {
          return !link.hidden && link.getClientRects().length > 0;
        });
      }

      function place() {
        if (!menu.open) return;
        const rect = summary.getBoundingClientRect();
        const width = Math.min(200, window.innerWidth - 16);
        const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
        panel.style.position = "fixed";
        panel.style.width = `${width}px`;
        panel.style.top = `${Math.round(rect.bottom + 8)}px`;
        panel.style.left = `${Math.round(left)}px`;
        panel.style.right = "auto";
        panel.style.zIndex = "30";
      }

      menu.addEventListener("toggle", place);
      window.addEventListener("resize", place);
      window.addEventListener("scroll", place, true);

      summary.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          menu.open = true;
          const first = visibleLinks()[0];
          if (first) first.focus();
        } else if (event.key === "Escape" && menu.open) {
          event.preventDefault();
          menu.open = false;
        }
      });

      panel.addEventListener("keydown", (event) => {
        const links = visibleLinks();
        const index = links.indexOf(document.activeElement);
        if (event.key === "Escape") {
          event.preventDefault();
          menu.open = false;
          summary.focus();
        } else if (event.key === "ArrowDown" && links.length) {
          event.preventDefault();
          links[(index + 1) % links.length].focus();
        } else if (event.key === "ArrowUp" && links.length) {
          event.preventDefault();
          if (index <= 0) {
            menu.open = false;
            summary.focus();
          } else {
            links[index - 1].focus();
          }
        }
      });

      document.addEventListener("click", (event) => {
        if (!menu.open || menu.contains(event.target)) return;
        menu.open = false;
      });
    });
  }

  function initOrder() {
    const niche = document.getElementById("niche");
    const customWrap = document.getElementById("custom-wrap");
    const customNiche = document.getElementById("custom-niche");
    const notes = document.getElementById("notes");
    const packages = Array.from(document.querySelectorAll("[data-package]"));
    const lengths = Array.from(document.querySelectorAll("[data-length]"));
    const formats = Array.from(document.querySelectorAll("[data-format]"));
    const error = document.getElementById("checkout-error");
    const banner = document.getElementById("stripe-banner");
    const total = document.getElementById("summary-total");
    const lines = {
      package: document.getElementById("summary-package"),
      length: document.getElementById("summary-length"),
      format: document.getElementById("summary-format"),
      niche: document.getElementById("summary-niche"),
    };
    let config = null;
    let selected = "";
    let length = "5";
    let format = "16:9";

    function showError(message) {
      error.hidden = false;
      error.textContent = message;
    }

    function priceFor(key) {
      const pkg = (config && config.packages || []).find((item) => item.key === key);
      if (!pkg) return null;
      return pkg.prices[length];
    }

    function render() {
      packages.forEach((btn) => {
        const cents = priceFor(btn.dataset.package);
        const slot = btn.querySelector("[data-price]");
        if (slot && cents != null) slot.textContent = money(cents);
        btn.setAttribute("aria-pressed", btn.dataset.package === selected ? "true" : "false");
      });
      lengths.forEach((btn) => btn.setAttribute("aria-pressed", btn.dataset.length === length ? "true" : "false"));
      formats.forEach((btn) => btn.setAttribute("aria-pressed", btn.dataset.format === format ? "true" : "false"));
      const pkg = (config && config.packages || []).find((item) => item.key === selected);
      lines.package.textContent = pkg ? `${pkg.name} · ${pkg.videos} videos` : "Select a package";
      lines.length.textContent = length === "10" ? "10-minute" : "5-minute";
      lines.format.textContent = format === "both" ? "Both" : format;
      const custom = niche.value === "Custom niche";
      customWrap.hidden = !custom;
      lines.niche.textContent = custom ? (customNiche.value.trim() || "Custom niche") : niche.value;
      const cents = selected ? priceFor(selected) : null;
      total.textContent = cents == null ? "—" : money(cents);
    }

    niche.addEventListener("change", render);
    customNiche.addEventListener("input", render);
    packages.forEach((btn) => btn.addEventListener("click", () => { selected = btn.dataset.package; render(); }));
    lengths.forEach((btn) => btn.addEventListener("click", () => { length = btn.dataset.length; render(); }));
    formats.forEach((btn) => btn.addEventListener("click", () => { format = btn.dataset.format; render(); }));

    document.getElementById("checkout").addEventListener("click", async () => {
      error.hidden = true;
      if (!config) { showError("Prices are still loading. Try again in a moment."); return; }
      if (!config.stripe_configured) { showError(config.stripe_error || "Stripe is not configured."); return; }
      if (!selected) { showError("Choose a package."); return; }
      if (niche.value === "Custom niche" && !customNiche.value.trim()) { showError("Enter a custom niche."); return; }
      const btn = document.getElementById("checkout");
      btn.disabled = true;
      try {
        const data = await api("/api/orders/checkout", {
          method: "POST",
          body: {
            niche: niche.value,
            custom_niche: customNiche.value,
            channel_notes: notes.value,
            package: selected,
            video_length: length,
            format: format,
          },
        });
        if (!data.url) throw new Error("Stripe did not return a checkout URL.");
        window.location.href = data.url;
      } catch (err) {
        showError(err.message || "Checkout failed.");
        btn.disabled = false;
      }
    });

    api("/api/orders/config").then((data) => {
      config = data;
      if (!data.stripe_configured) {
        banner.hidden = false;
        banner.textContent = data.stripe_error || "Stripe is not configured.";
        banner.classList.add("warn");
      } else if (data.stripe_mode === "test") {
        banner.hidden = false;
        banner.textContent = "Stripe test mode. Use a test card such as 4242 4242 4242 4242.";
      } else if (data.stripe_mode === "live") {
        banner.hidden = false;
        banner.textContent = "Stripe live mode. This checkout charges a real card.";
        banner.classList.add("warn");
      }
      render();
    }).catch((err) => {
      showError(err.message || "Could not load prices.");
    });
    render();
  }

  function initSuccess() {
    const slot = document.getElementById("thanks");
    const params = new URLSearchParams(window.location.search);
    const sid = params.get("session_id") || "";
    if (!sid) {
      slot.textContent = "Payment received. Open My Orders with your checkout email if the order id is not on this page yet.";
      return;
    }
    let tries = 0;
    function paint(order) {
      slot.textContent = "";
      const title = document.createElement("p");
      title.textContent = "Order " + order.id + " is " + String(order.status || "paid").replaceAll("_", " ") + ".";
      const detail = document.createElement("p");
      detail.textContent = `${order.package_name || "Package"} · ${money(order.amount_cents)}`;
      slot.append(title, detail);
    }
    function poll() {
      tries += 1;
      api("/api/orders/by-session/" + encodeURIComponent(sid)).then((data) => {
        if (data.order) { paint(data.order); return; }
        if (tries >= 15) {
          slot.textContent = "Payment is in. The order record is still catching up — refresh this page, or check My Orders with your checkout email.";
          return;
        }
        setTimeout(poll, 2000);
      }).catch((err) => {
        slot.textContent = err.message || "Could not look up this checkout session.";
      });
    }
    slot.textContent = "Confirming your payment…";
    poll();
  }

  function initMine() {
    const form = document.getElementById("lookup");
    const input = document.getElementById("lookup-email");
    const error = document.getElementById("lookup-error");
    const results = document.getElementById("results");
    const preset = new URLSearchParams(window.location.search).get("email") || "";
    if (preset) input.value = preset;

    function render(orders) {
      results.textContent = "";
      if (!orders.length) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.textContent = "No orders for that email.";
        results.append(empty);
        return;
      }
      orders.forEach((order) => {
        const card = document.createElement("article");
        card.className = "card order";
        const heading = document.createElement("h2");
        heading.textContent = `${order.package_name} · ${order.id}`;
        const meta = document.createElement("p");
        meta.textContent = `${order.niche} · ${order.video_length_label} · ${order.format_label} · ${money(order.amount_cents)} · ${String(order.status || "").replaceAll("_", " ")}`;
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.setAttribute("role", "progressbar");
        bar.setAttribute("aria-valuenow", String(order.progress_pct || 0));
        bar.setAttribute("aria-valuemin", "0");
        bar.setAttribute("aria-valuemax", "100");
        const fill = document.createElement("span");
        fill.style.width = `${order.progress_pct || 0}%`;
        bar.append(fill);
        card.append(heading, meta, bar);
        if (order.delivery_message) {
          const note = document.createElement("p");
          note.className = "note";
          note.textContent = order.delivery_message;
          card.append(note);
        }
        (order.videos || []).forEach((video) => {
          const row = document.createElement("div");
          row.className = "video";
          const title = document.createElement("strong");
          title.textContent = video.topic || `Video ${video.position}`;
          const state = document.createElement("span");
          state.className = "status";
          state.textContent = video.status;
          row.append(title, document.createTextNode(" · "), state);
          if (video.download_url) {
            const link = document.createElement("a");
            link.className = "btn-ghost";
            link.href = BASE + video.download_url;
            link.textContent = "Download MP4";
            row.append(document.createTextNode(" "), link);
          }
          card.append(row);
        });
        results.append(card);
      });
    }

    function lookup(event) {
      if (event) event.preventDefault();
      error.hidden = true;
      api("/api/my-orders", { method: "POST", body: { email: input.value } })
        .then((data) => render(data.orders || []))
        .catch((err) => {
          error.hidden = false;
          error.textContent = err.message || "Lookup failed.";
        });
    }
    form.addEventListener("submit", lookup);
    if (preset) lookup();
  }

  function initProduction() {
    const root = document.getElementById("queue");
    const badge = document.getElementById("unreviewed");
    const error = document.getElementById("queue-error");
    const openIds = new Set();
    const STATUSES = ["paid", "queued", "in_production", "delivered"];
    const VIDEO = ["queued", "scripting", "art", "narration", "rendering", "ready"];

    function fail(err) {
      error.hidden = false;
      error.textContent = err.message || "Could not update the queue.";
    }

    function load() {
      error.hidden = true;
      api("/api/production/orders").then(paint).catch(fail);
    }

    function paint(data) {
      const count = Number(data.unreviewed_count || 0);
      badge.hidden = count <= 0;
      badge.textContent = count ? `${count} new` : "";
      root.textContent = "";
      const orders = data.orders || [];
      if (!orders.length) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.textContent = "No paid orders yet.";
        root.append(empty);
        return;
      }
      orders.forEach((order) => root.append(renderOrder(order)));
    }

    function renderOrder(order) {
      const details = document.createElement("details");
      details.className = "card order";
      if (openIds.has(order.id)) details.open = true;
      details.addEventListener("toggle", () => {
        if (details.open) openIds.add(order.id);
        else openIds.delete(order.id);
        if (details.open && !order.reviewed) {
          order.reviewed = true;
          api(`/api/production/orders/${encodeURIComponent(order.id)}/review`, { method: "POST", body: {} })
            .then((data) => {
              const left = Number(data.unreviewed_count || 0);
              badge.hidden = left <= 0;
              badge.textContent = left ? `${left} new` : "";
            }).catch(() => {});
        }
      });
      const summary = document.createElement("summary");
      summary.textContent = `${order.name || "Client"} · ${order.package_name} · ${money(order.amount_cents)} · ${order.status.replaceAll("_", " ")}`;
      details.append(summary);
      const meta = document.createElement("div");
      meta.className = "order-meta";
      [
        ["Name", order.name],
        ["Email", order.email],
        ["Niche", order.niche],
        ["Package", `${order.package_name} (${order.video_count})`],
        ["Length", order.video_length_label],
        ["Format", order.format_label],
        ["Paid", money(order.amount_cents)],
      ].forEach(([label, value]) => {
        const cell = document.createElement("p");
        const name = document.createElement("span");
        name.className = "meta-label";
        name.textContent = label;
        cell.append(name, document.createTextNode(value || "—"));
        meta.append(cell);
      });
      details.append(meta);
      if (order.channel_notes) {
        const notes = document.createElement("p");
        notes.textContent = order.channel_notes;
        details.append(notes);
      }
      if (order.delivery_note) {
        const note = document.createElement("p");
        note.className = "note";
        note.textContent = order.delivery_note;
        details.append(note);
      }
      if (order.delivery_email_error) {
        const mailError = document.createElement("p");
        mailError.className = "error";
        mailError.textContent = "Email error: " + order.delivery_email_error;
        details.append(mailError);
      }
      const statusRow = document.createElement("div");
      statusRow.className = "row-actions";
      const select = document.createElement("select");
      select.setAttribute("aria-label", "Order status");
      STATUSES.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value.replaceAll("_", " ");
        option.selected = value === order.status;
        if (value === "delivered" && !order.can_deliver) option.disabled = true;
        select.append(option);
      });
      const update = document.createElement("button");
      update.type = "button";
      update.className = "ghost";
      update.textContent = "Update status";
      update.addEventListener("click", () => {
        api(`/api/production/orders/${encodeURIComponent(order.id)}/status`, {
          method: "POST",
          body: { status: select.value },
        }).then(load).catch(fail);
      });
      const deliver = document.createElement("button");
      deliver.type = "button";
      deliver.className = "primary";
      if (order.status === "delivered" && order.email_sent) {
        deliver.textContent = "Delivered";
        deliver.disabled = true;
      } else if (order.status === "delivered") {
        deliver.textContent = "Retry delivery email";
        deliver.disabled = !order.can_deliver;
      } else {
        deliver.textContent = "Mark delivered";
        deliver.disabled = !order.can_deliver;
      }
      deliver.addEventListener("click", () => {
        if (deliver.disabled) return;
        api(`/api/production/orders/${encodeURIComponent(order.id)}/deliver`, { method: "POST", body: {} })
          .then(load).catch(fail);
      });
      statusRow.append(select, update, deliver);
      if (!order.can_deliver && order.status !== "delivered") {
        const why = document.createElement("p");
        why.className = "hint";
        why.textContent = "Mark delivered unlocks when every video is ready and has an MP4.";
        details.append(statusRow, why);
      } else {
        details.append(statusRow);
      }
      (order.videos || []).forEach((video) => details.append(renderVideo(order, video)));
      return details;
    }

    function renderVideo(order, video) {
      const wrap = document.createElement("div");
      wrap.className = "video";
      const label = document.createElement("strong");
      label.textContent = `Video ${video.position}`;
      const topic = document.createElement("input");
      topic.value = video.topic || "";
      topic.placeholder = "Topic Ely picks";
      topic.setAttribute("aria-label", `Topic for video ${video.position}`);
      const save = document.createElement("button");
      save.type = "button";
      save.className = "ghost";
      save.textContent = "Save topic";
      save.addEventListener("click", () => {
        api(`/api/production/orders/${encodeURIComponent(order.id)}/videos/${encodeURIComponent(video.id)}`, {
          method: "POST",
          body: { topic: topic.value },
        }).then(load).catch(fail);
      });
      const status = document.createElement("select");
      status.setAttribute("aria-label", `Status for video ${video.position}`);
      VIDEO.forEach((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.selected = value === video.status;
        if (value === "ready" && !video.mp4_attached) option.disabled = true;
        status.append(option);
      });
      status.addEventListener("change", () => {
        api(`/api/production/orders/${encodeURIComponent(order.id)}/videos/${encodeURIComponent(video.id)}`, {
          method: "POST",
          body: { status: status.value },
        }).then(load).catch(fail);
      });
      const file = document.createElement("input");
      file.type = "file";
      file.accept = "video/mp4,.mp4";
      file.setAttribute("aria-label", `MP4 for video ${video.position}`);
      const upload = document.createElement("button");
      upload.type = "button";
      upload.className = "ghost";
      upload.textContent = video.mp4_attached ? "Replace MP4" : "Upload MP4";
      upload.addEventListener("click", () => {
        if (!file.files || !file.files[0]) { fail(new Error("Choose an MP4 file first.")); return; }
        const body = new FormData();
        body.append("file", file.files[0]);
        api(`/api/production/orders/${encodeURIComponent(order.id)}/videos/${encodeURIComponent(video.id)}/upload`, {
          method: "POST",
          body: body,
        }).then(load).catch(fail);
      });
      const ready = document.createElement("button");
      ready.type = "button";
      ready.className = "ghost";
      ready.textContent = video.status === "ready" ? "Ready" : "Mark ready";
      ready.disabled = video.status === "ready" || !video.mp4_attached;
      ready.addEventListener("click", () => {
        api(`/api/production/orders/${encodeURIComponent(order.id)}/videos/${encodeURIComponent(video.id)}/ready`, {
          method: "POST",
          body: {},
        }).then(load).catch(fail);
      });
      const row = document.createElement("div");
      row.className = "video-actions";
      row.append(topic, save, status, file, upload, ready);
      if (video.mp4_url) {
        const link = document.createElement("a");
        link.href = BASE + video.mp4_url;
        link.textContent = "Download";
        row.append(link);
      }
      wrap.append(label, row);
      return wrap;
    }

    load();
  }
})();
