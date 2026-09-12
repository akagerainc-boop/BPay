/* BPay admin dashboard.
 *
 * Everything the Flutter app dials is defined here: which USSD code runs
 * for each SIM network and transaction type, and where the user's own
 * input is substituted into it.
 */

const API = "";               // same origin as this page
let token = localStorage.getItem("bpay_admin_token") || null;

const TRANSACTION_TYPES = [
  ["phone_transfer", "Phone transfer"],
  ["merchant_payment", "Merchant payment"],
  ["bill_payment", "Bill payment"],
  ["airtime", "Airtime"],
  ["mokash_send", "MoKash — send"],
  ["mokash_withdraw", "MoKash — withdraw to SIM"],
  ["check_balance", "Check balance"],
];

// Icons the Flutter app knows how to render. Anything else falls back to
// a default icon in the app rather than showing nothing.
const FLUTTER_ICONS = [
  "bolt_rounded", "water_drop_rounded", "phone_android_rounded",
  "wifi_rounded", "tv_rounded", "account_balance_rounded",
  "account_balance_outlined", "account_balance_wallet_rounded",
  "school_outlined", "shield_outlined", "local_hospital_rounded",
  "directions_bus_rounded", "receipt_long_rounded", "storefront_outlined",
  "shopping_bag_outlined", "sim_card_rounded", "send_rounded",
];

/* ------------------------------------------------------------- helpers */
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(API + path, { ...options, headers });

  if (res.status === 401) {
    signOut();
    throw new Error("Session expired — sign in again");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 2600);
}

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

const money = (n) => new Intl.NumberFormat("en-US").format(n || 0) + " RWF";

/* --------------------------------------------------------------- auth */
document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("login-error");
  errorEl.hidden = true;
  try {
    const data = await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.getElementById("login-username").value,
        password: document.getElementById("login-password").value,
      }),
    });
    token = data.token;
    localStorage.setItem("bpay_admin_token", token);
    localStorage.setItem("bpay_admin_user", data.username);
    showApp();
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  }
});

function signOut() {
  token = null;
  localStorage.removeItem("bpay_admin_token");
  document.getElementById("app-view").hidden = true;
  document.getElementById("login-view").hidden = false;
}

document.getElementById("logout").addEventListener("click", signOut);

/* -------------------------------------------------------------- theme */
const themeToggleBtn = document.getElementById("theme-toggle");

function currentTheme() {
  const saved = document.documentElement.dataset.theme;
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("bpay_admin_theme", theme); } catch (_) {}
  themeToggleBtn.textContent = theme === "dark" ? "☀️" : "🌙";
  themeToggleBtn.title = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
}

themeToggleBtn.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
});

applyTheme(currentTheme());

function showApp() {
  document.getElementById("login-view").hidden = true;
  document.getElementById("app-view").hidden = false;
  document.getElementById("who").textContent =
    localStorage.getItem("bpay_admin_user") || "";
  loadOverview();
  loadTemplates();
  loadServices();
}

/* --------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = true; });
    document.getElementById(`tab-${tab.dataset.tab}`).hidden = false;
    if (tab.dataset.tab === "transactions") loadTransactions();
    if (tab.dataset.tab === "overview") loadOverview();
    if (tab.dataset.tab === "fees") { loadFeeRules(); loadProviderKeys(); }
    if (tab.dataset.tab === "announcements") loadAnnouncements();
    if (tab.dataset.tab === "users") loadUsers();
  });
});

/* ----------------------------------------------------------- overview */
async function loadOverview() {
  try {
    const s = await api("/api/admin/stats");
    document.getElementById("stats").innerHTML = `
      <div class="stat"><div class="value">${s.transactions}</div><div class="label">Transactions reported</div></div>
      <div class="stat"><div class="value">${s.successful}</div><div class="label">Reported successful</div></div>
      <div class="stat"><div class="value">${money(s.volume)}</div><div class="label">Reported volume</div></div>
      <div class="stat"><div class="value">${s.templates}</div><div class="label">Active USSD codes</div></div>
      <div class="stat"><div class="value">${s.services}</div><div class="label">Active services</div></div>
      <div class="stat"><div class="value">${s.devices}</div><div class="label">Devices registered</div></div>`;
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------------------------------------------- ussd templates */
let templates = [];

async function loadTemplates() {
  try {
    templates = await api("/api/admin/ussd-templates");
    renderTemplates();
  } catch (err) {
    toast(err.message);
  }
}

function renderTemplates() {
  const tbody = document.querySelector("#templates-table tbody");
  if (!templates.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No templates yet — add one so the app knows what to dial.</td></tr>`;
    return;
  }
  const label = (v) => (TRANSACTION_TYPES.find((t) => t[0] === v) || [v, v])[1];
  tbody.innerHTML = templates.map((t) => `
    <tr>
      <td><strong>${escapeHtml(t.sim_network.toUpperCase())}</strong></td>
      <td>${escapeHtml(label(t.transaction_type))}</td>
      <td>${escapeHtml(t.recipient_network)}</td>
      <td class="mono">${escapeHtml(t.template)}</td>
      <td><span class="pill ${t.completes_payment ? "on" : "warn"}">${t.completes_payment ? "Yes" : "Menu only"}</span></td>
      <td><span class="pill ${t.active ? "on" : "off"}">${t.active ? "Active" : "Off"}</span></td>
      <td><div class="row-actions">
        <button class="btn ghost small" onclick="editTemplate(${t.id})">Edit</button>
        <button class="btn danger small" onclick="deleteTemplate(${t.id})">Delete</button>
      </div></td>
    </tr>`).join("");
}

function templateForm(t = {}) {
  const opts = (list, selected) => list
    .map(([v, l]) => `<option value="${v}" ${v === selected ? "selected" : ""}>${l}</option>`)
    .join("");
  return `
    <label>Paying SIM network
      <select name="sim_network">
        ${opts([["mtn", "MTN"], ["airtel", "Airtel"]], t.sim_network || "mtn")}
      </select>
    </label>
    <label>Transaction type
      <select name="transaction_type">${opts(TRANSACTION_TYPES, t.transaction_type || "phone_transfer")}</select>
    </label>
    <label>Recipient network
      <select name="recipient_network">
        ${opts([["any", "Any"], ["mtn", "MTN"], ["airtel", "Airtel"]], t.recipient_network || "any")}
      </select>
      <span class="field-hint">Only matters for phone transfers — pick "Any" otherwise.</span>
    </label>
    <label>USSD template
      <input type="text" name="template" value="${escapeHtml(t.template || "")}" placeholder="*182*1*1*{recipient}*{amount}#" required>
      <span class="field-hint">
        Placeholders: <code>{recipient}</code> <code>{amount}</code>
        <code>{code}</code> <code>{account}</code> — the app substitutes the
        user's input wherever you put them.
      </span>
    </label>
    <label class="checkline">
      <input type="checkbox" name="completes_payment" ${t.completes_payment !== false ? "checked" : ""}>
      Completes payment (code carries the amount and destination)
    </label>
    <label>Guidance if it only opens a menu
      <textarea name="guidance" placeholder="Choose Send Money, then enter the number shown above.">${escapeHtml(t.guidance || "")}</textarea>
    </label>
    <label class="checkline">
      <input type="checkbox" name="active" ${t.active !== false ? "checked" : ""}> Active
    </label>`;
}

window.editTemplate = (id) => {
  const t = templates.find((x) => x.id === id);
  openModal("Edit USSD template", templateForm(t), async (values) => {
    await api(`/api/admin/ussd-templates/${id}`, { method: "PUT", body: JSON.stringify(values) });
    toast("Template updated — the app picks this up on next refresh");
    loadTemplates();
  });
};

window.deleteTemplate = async (id) => {
  if (!confirm("Delete this USSD template? The app will stop offering that route.")) return;
  try {
    await api(`/api/admin/ussd-templates/${id}`, { method: "DELETE" });
    toast("Template deleted");
    loadTemplates();
  } catch (err) { toast(err.message); }
};

document.getElementById("add-template").addEventListener("click", () => {
  openModal("Add USSD template", templateForm(), async (values) => {
    await api("/api/admin/ussd-templates", { method: "POST", body: JSON.stringify(values) });
    toast("Template added");
    loadTemplates();
  });
});

/* --------------------------------------------------------- services */
let services = [];

async function loadServices() {
  try {
    services = await api("/api/admin/services");
    renderServices();
  } catch (err) { toast(err.message); }
}

function renderServices() {
  const tbody = document.querySelector("#services-table tbody");
  if (!services.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty">No services yet.</td></tr>`;
    return;
  }
  const nets = (s) => {
    const has = [];
    if (s.ussd_template_mtn) has.push("MTN");
    if (s.ussd_template_airtel) has.push("Airtel");
    return has.length ? has.join(" + ") : "—";
  };
  tbody.innerHTML = services.map((s) => `
    <tr>
      <td class="mono">${escapeHtml(s.icon)}</td>
      <td><strong>${escapeHtml(s.name)}</strong><br><span class="muted">${escapeHtml(s.description || "")}</span></td>
      <td class="mono">${escapeHtml(s.ussd_template_mtn || "—")}</td>
      <td class="mono">${escapeHtml(s.ussd_template_airtel || "—")}</td>
      <td>${escapeHtml(s.account_label)}</td>
      <td>${nets(s)}</td>
      <td>${s.sort_order}</td>
      <td><span class="pill ${s.active ? "on" : "off"}">${s.active ? "Active" : "Off"}</span></td>
      <td><div class="row-actions">
        <button class="btn ghost small" onclick="editService(${s.id})">Edit</button>
        <button class="btn danger small" onclick="deleteService(${s.id})">Delete</button>
      </div></td>
    </tr>`).join("");
}

function serviceForm(s = {}) {
  const iconOpts = FLUTTER_ICONS
    .map((i) => `<option value="${i}" ${i === s.icon ? "selected" : ""}>${i}</option>`)
    .join("");
  return `
    <label>Name<input type="text" name="name" value="${escapeHtml(s.name || "")}" required></label>
    <label>Description<input type="text" name="description" value="${escapeHtml(s.description || "")}"></label>
    <label>Category<input type="text" name="category" value="${escapeHtml(s.category || "other")}"></label>
    <label>Flutter icon
      <select name="icon">${iconOpts}</select>
      <span class="field-hint">Rendered in the app's service grid.</span>
    </label>
    <label>MTN USSD template
      <input type="text" name="ussd_template_mtn" value="${escapeHtml(s.ussd_template_mtn || "")}" placeholder="*182*2*6*{account}*{amount}#">
      <span class="field-hint">
        Leave blank if this service isn't offered to MTN users. Use
        <code>{account}</code> for the meter/account number and
        <code>{amount}</code> for the amount.
      </span>
    </label>
    <label>Airtel USSD template
      <input type="text" name="ussd_template_airtel" value="${escapeHtml(s.ussd_template_airtel || "")}" placeholder="*185*...#">
      <span class="field-hint">
        MTN and Airtel almost never share a code for the same service — set
        this independently. Leave blank if not offered to Airtel users.
      </span>
    </label>
    <label>Account field label
      <input type="text" name="account_label" value="${escapeHtml(s.account_label || "Account number")}">
      <span class="field-hint">What the app calls this field — "Meter number", "Decoder number"…</span>
    </label>
    <label>Sort order
      <input type="number" name="sort_order" value="${s.sort_order ?? 0}">
      <span class="field-hint">Higher shows first — use this for the most frequently used services.</span>
    </label>
    <label class="checkline">
      <input type="checkbox" name="active" ${s.active !== false ? "checked" : ""}> Active
    </label>`;
}

window.editService = (id) => {
  const s = services.find((x) => x.id === id);
  openModal("Edit service", serviceForm(s), async (values) => {
    await api(`/api/admin/services/${id}`, { method: "PUT", body: JSON.stringify(values) });
    toast("Service updated");
    loadServices();
  });
};

window.deleteService = async (id) => {
  if (!confirm("Delete this service? It disappears from the app.")) return;
  try {
    await api(`/api/admin/services/${id}`, { method: "DELETE" });
    toast("Service deleted");
    loadServices();
  } catch (err) { toast(err.message); }
};

document.getElementById("add-service").addEventListener("click", () => {
  openModal("Add service", serviceForm(), async (values) => {
    await api("/api/admin/services", { method: "POST", body: JSON.stringify(values) });
    toast("Service added");
    loadServices();
  });
});

/* --------------------------------------------------------- fee rules */
const WINDOW_LABELS = { day: "day", week: "week", month: "month", year: "year" };

async function loadFeeRules() {
  try {
    const rules = await api("/api/admin/fee-rules");
    const container = document.getElementById("fee-rules");
    container.innerHTML = rules.map((r) => `
      <div class="fee-card" data-network="${r.network}">
        <h4>${r.network.toUpperCase()}</h4>
        <label>Fee amount (RWF)
          <input type="number" min="0" class="fee-amount" value="${r.fee_amount}">
        </label>
        <label>After how many transactions
          <input type="number" min="1" class="fee-count" value="${r.trigger_count}">
        </label>
        <label>Per
          <select class="fee-window">
            ${Object.entries(WINDOW_LABELS).map(([v, l]) =>
              `<option value="${v}" ${v === r.trigger_window ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        </label>
        <label class="checkline">
          <input type="checkbox" class="fee-active" ${r.active ? "checked" : ""}>
          Show this fee notice
        </label>
        <button class="btn primary small" onclick="saveFeeRule('${r.network}')">Save</button>
      </div>`).join("");
  } catch (err) { toast(err.message); }
}

window.saveFeeRule = async (network) => {
  const card = document.querySelector(`.fee-card[data-network="${network}"]`);
  const body = {
    fee_amount: Number(card.querySelector(".fee-amount").value || 0),
    trigger_count: Number(card.querySelector(".fee-count").value || 1),
    trigger_window: card.querySelector(".fee-window").value,
    active: card.querySelector(".fee-active").checked,
  };
  try {
    await api(`/api/admin/fee-rules/${network}`, { method: "PUT", body: JSON.stringify(body) });
    toast(`${network.toUpperCase()} fee rule saved`);
  } catch (err) { toast(err.message); }
};

/* ----------------------------------------------------- provider keys */
// What each network's Collections API actually needs — see
// momo_client.py / airtel_client.py on the backend for what reads these.
const PROVIDER_FIELD_DEFS = {
  mtn: [
    { key: "subscription_key", label: "Subscription key (Ocp-Apim-Subscription-Key)", secret: true },
    { key: "api_user", label: "API user (UUID)", secret: false },
    { key: "api_key", label: "API key", secret: true },
  ],
  airtel: [
    { key: "client_id", label: "Client ID", secret: false },
    { key: "client_secret", label: "Client secret", secret: true },
  ],
};

async function loadProviderKeys() {
  try {
    const rows = await api("/api/admin/provider-keys");
    const container = document.getElementById("provider-keys");
    container.innerHTML = rows.map((r) => {
      const defs = PROVIDER_FIELD_DEFS[r.network] || [];
      const fieldsHtml = defs.map((d) => {
        const current = r.fields[d.key];
        if (d.secret) {
          return `
            <label>${d.label}
              ${current ? `<span class="key-preview">${escapeHtml(current)}</span>` : ""}
              <input type="password" class="pk-field" data-field="${d.key}"
                     placeholder="${current ? "Paste to replace — leave blank to keep current" : "Not set"}"
                     autocomplete="off">
            </label>`;
        }
        return `
          <label>${d.label}
            <input type="text" class="pk-field" data-field="${d.key}" value="${escapeHtml(current || "")}">
          </label>`;
      }).join("");

      return `
        <div class="key-card" data-network="${r.network}">
          <h4>${r.network.toUpperCase()}
            <span class="pill ${r.configured ? "on" : "off"}">${r.configured ? "Configured" : "Incomplete"}</span>
          </h4>
          <label>Environment
            <select class="pk-field" data-field="environment">
              <option value="sandbox" ${r.environment === "sandbox" ? "selected" : ""}>Sandbox</option>
              <option value="production" ${r.environment === "production" ? "selected" : ""}>Production</option>
            </select>
          </label>
          <label>Base URL <span class="field-hint">(blank = the provider's default sandbox URL)</span>
            <input type="text" class="pk-field" data-field="base_url" value="${escapeHtml(r.base_url || "")}"
                   placeholder="${r.network === "mtn" ? "https://sandbox.momodeveloper.mtn.com" : "https://openapiuat.airtel.africa"}">
          </label>
          ${r.network === "mtn" ? `
          <label>Target environment <span class="field-hint">(X-Target-Environment — "sandbox", or whatever MTN assigned for production)</span>
            <input type="text" class="pk-field" data-field="target_environment" value="${escapeHtml(r.target_environment || "")}" placeholder="sandbox">
          </label>` : ""}
          ${fieldsHtml}
          <button class="btn primary small" onclick="saveProviderKey('${r.network}')">Save</button>
        </div>`;
    }).join("");
  } catch (err) { toast(err.message); }
}

window.saveProviderKey = async (network) => {
  const card = document.querySelector(`.key-card[data-network="${network}"]`);
  const body = {};
  card.querySelectorAll(".pk-field").forEach((el) => { body[el.dataset.field] = el.value; });
  try {
    await api(`/api/admin/provider-keys/${network}`, { method: "PUT", body: JSON.stringify(body) });
    toast(`${network.toUpperCase()} credentials saved`);
    loadProviderKeys();
  } catch (err) { toast(err.message); }
};

/* --------------------------------------------------- announcements */
async function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch("/api/admin/uploads", { method: "POST", headers, body: formData });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
  return data.url;
}

let annPhotoUrl = null;
let annLogoUrl = null;

document.getElementById("ann-photo-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    annPhotoUrl = await uploadFile(file);
    const img = document.getElementById("ann-photo-preview");
    img.src = annPhotoUrl;
    img.hidden = false;
  } catch (err) { toast(err.message); }
});

document.getElementById("ann-logo-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    annLogoUrl = await uploadFile(file);
    const img = document.getElementById("ann-logo-preview");
    img.src = annLogoUrl;
    img.hidden = false;
  } catch (err) { toast(err.message); }
});

document.getElementById("ann-push").addEventListener("click", async () => {
  const title = document.getElementById("ann-title").value.trim();
  const message = document.getElementById("ann-message").value.trim();
  if (!title || !message) { toast("Title and message are required"); return; }

  const btn = document.getElementById("ann-push");
  btn.disabled = true;
  try {
    const result = await api("/api/admin/announcements", {
      method: "POST",
      body: JSON.stringify({ title, message, photo_url: annPhotoUrl, logo_url: annLogoUrl }),
    });
    toast(
      result.error
        ? `Saved, but the push failed: ${result.error}`
        : `Pushed to ${result.sent_count} device${result.sent_count === 1 ? "" : "s"}`
    );
    document.getElementById("ann-title").value = "";
    document.getElementById("ann-message").value = "";
    annPhotoUrl = null;
    annLogoUrl = null;
    document.getElementById("ann-photo-preview").hidden = true;
    document.getElementById("ann-logo-preview").hidden = true;
    document.getElementById("ann-photo-file").value = "";
    document.getElementById("ann-logo-file").value = "";
    loadAnnouncements();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
});

async function loadAnnouncements() {
  try {
    const data = await api("/api/admin/announcements");
    const statusEl = document.getElementById("firebase-status");
    statusEl.textContent = data.firebase_configured
      ? "Firebase is configured — pushes go out to every registered device."
      : `Firebase is not configured yet: ${data.firebase_error}`;
    statusEl.style.color = data.firebase_configured ? "" : "var(--error)";

    const tbody = document.querySelector("#announcements-table tbody");
    if (!data.announcements.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Nothing sent yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.announcements.map((a) => `
      <tr>
        <td>${escapeHtml(a.created_at)}</td>
        <td><strong>${escapeHtml(a.title)}</strong></td>
        <td>${escapeHtml((a.message || "").slice(0, 80))}${(a.message || "").length > 80 ? "…" : ""}</td>
        <td>${a.sent_count}</td>
        <td class="row-actions">
          <button class="btn ghost small" onclick="resendAnnouncement(${a.id}, this)">Resend</button>
        </td>
      </tr>`).join("");
  } catch (err) { toast(err.message); }
}

window.resendAnnouncement = async (id, btn) => {
  btn.disabled = true;
  try {
    const result = await api(`/api/admin/announcements/${id}/resend`, { method: "POST" });
    toast(
      result.error
        ? `Resend failed: ${result.error}`
        : `Resent to ${result.sent_count} device${result.sent_count === 1 ? "" : "s"}`
    );
    loadAnnouncements();
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
  }
};

/* ------------------------------------------------------------- users */
async function loadUsers() {
  try {
    const rows = await api("/api/admin/users");
    const tbody = document.querySelector("#users-table tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="empty">No devices yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((u) => `
      <tr>
        <td class="mono">${escapeHtml(u.device_id.slice(0, 12))}…</td>
        <td>${u.phone ? escapeHtml(u.phone) : `<span class="muted">Not shared</span>`}</td>
        <td>${u.sim_network ? escapeHtml(u.sim_network.toUpperCase()) : "—"}</td>
        <td><span class="pill ${u.has_push_token ? "on" : "off"}">${u.has_push_token ? "On" : "Off"}</span></td>
        <td>${u.transaction_count}</td>
        <td>${u.successful_count}</td>
        <td>${u.failed_count > 0
          ? `<span class="pill err">${u.failed_count}</span> <span class="muted">${money(u.failed_volume)}</span>`
          : "0"}</td>
        <td>${money(u.total_volume)}</td>
        <td>${escapeHtml(u.last_transaction_at || u.last_seen || "—")}</td>
      </tr>`).join("");
  } catch (err) { toast(err.message); }
}

document.getElementById("refresh-users").addEventListener("click", loadUsers);

/* ----------------------------------------------------- transactions */
async function loadTransactions() {
  try {
    const rows = await api("/api/admin/transactions?limit=200");
    const tbody = document.querySelector("#tx-table tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">No transactions reported yet.</td></tr>`;
      return;
    }
    const statusPill = (s) => {
      if (s === "success") return "on";
      if (["failed", "cancelled", "network_error"].includes(s)) return "err";
      return "warn";
    };
    tbody.innerHTML = rows.map((t) => `
      <tr>
        <td>${escapeHtml(t.created_at)}</td>
        <td class="mono">${escapeHtml(t.bpay_id)}</td>
        <td>${escapeHtml(t.type)}</td>
        <td>${escapeHtml(t.destination_name || t.destination || "—")}</td>
        <td>${money(t.amount)}</td>
        <td><span class="pill ${statusPill(t.status)}">${escapeHtml(t.status)}</span></td>
        <td class="muted">${escapeHtml(t.verification)}</td>
      </tr>`).join("");
  } catch (err) { toast(err.message); }
}

document.getElementById("refresh-tx").addEventListener("click", loadTransactions);

/* ---------------------------------------------------------- modal */
let modalSubmit = null;

function openModal(title, html, onSubmit) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-form").innerHTML = html;
  document.getElementById("modal-error").hidden = true;
  document.getElementById("modal").hidden = false;
  modalSubmit = onSubmit;
}

function closeModal() {
  document.getElementById("modal").hidden = true;
  modalSubmit = null;
}

document.getElementById("modal-cancel").addEventListener("click", closeModal);

document.getElementById("modal-save").addEventListener("click", async () => {
  // Nothing to submit unless a modal was actually opened with a handler.
  if (!modalSubmit) return closeModal();
  const form = document.getElementById("modal-form");
  const values = {};
  form.querySelectorAll("input, select, textarea").forEach((el) => {
    values[el.name] = el.type === "checkbox" ? el.checked : el.value;
  });
  const errorEl = document.getElementById("modal-error");
  errorEl.hidden = true;
  try {
    await modalSubmit(values);
    closeModal();
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.hidden = false;
  }
});

/* ---------------------------------------------------------- startup */
if (token) showApp();
