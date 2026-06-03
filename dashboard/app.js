"use strict";

/* Encrypted-static dashboard.
 *
 * Flow: fetch data.enc.json (an AES-256-GCM envelope produced by
 * src/export_dashboard.py) -> derive a key from the passphrase with PBKDF2
 * -> decrypt in-browser -> render. No data is readable without the passphrase,
 * and nothing ever leaves the device. Crypto parameters mirror the exporter.
 */

const DATA_URL = "data.enc.json";

const $ = (sel, root = document) => root.querySelector(sel);
const b64ToBytes = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

let DATA = null;          // decrypted payload
let SELECTED = null;      // currently selected month (YYYY-MM)

// ---- crypto ---------------------------------------------------------------

async function decryptPayload(envelope, passphrase) {
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    "raw", enc.encode(passphrase), "PBKDF2", false, ["deriveKey"],
  );
  const key = await crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      salt: b64ToBytes(envelope.salt),
      iterations: envelope.iterations,
      hash: "SHA-256",
    },
    baseKey,
    { name: "AES-GCM", length: (envelope.keyLenBytes || 32) * 8 },
    false,
    ["decrypt"],
  );
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: b64ToBytes(envelope.iv) },
    key,
    b64ToBytes(envelope.ciphertext),
  );
  return JSON.parse(new TextDecoder().decode(plaintext));
}

// ---- formatting -----------------------------------------------------------

const gbpExact = (pennies) =>
  new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" })
    .format((pennies || 0) / 100);

// Compact for axis-like values (no pence): £1,349, +£296
const gbp0 = (pennies, { sign = false } = {}) => {
  const s = new Intl.NumberFormat("en-GB", {
    style: "currency", currency: "GBP", maximumFractionDigits: 0,
  }).format(Math.abs((pennies || 0) / 100)) ;
  const neg = pennies < 0;
  return (neg ? "−" : (sign ? "+" : "")) + s;
};

const pct = (frac) => frac == null ? "—" : `${(frac * 100).toFixed(1)}%`;

const monthLabel = (ym) => {
  const [y, m] = ym.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1))
    .toLocaleString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
};

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (ch) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
));

// Reusable horizontal-bar list (categories, merchants, income, subscriptions).
function barList(items, { color, max, empty }) {
  if (!items.length) return `<p class="empty">${empty}</p>`;
  const top = max || Math.max(1, ...items.map((i) => i.value));
  return items.map((i) => `
    <div class="bar-row">
      <span class="name" title="${escapeHtml(i.label)}">${escapeHtml(i.label)}</span>
      <span class="track"><span class="fill" style="width:${(i.value / top) * 100}%;background:${color}"></span></span>
      <span class="amt">${gbpExact(i.value)}${i.sub ? ` <span class="muted tiny">${escapeHtml(i.sub)}</span>` : ""}</span>
    </div>`).join("");
}

// ---- month-specific panels ------------------------------------------------

function renderKpis(month) {
  const m = DATA.by_month[month] || { net_pennies: 0, savings_rate: null, txn_count: 0 };
  const nw = DATA.net_worth;
  const cards = [
    {
      label: "Net worth",
      value: gbp0(nw.known_pennies),
      cls: nw.known_pennies >= 0 ? "pos" : "neg",
      note: nw.has_unconnected ? "excl. accounts still pending" : "",
    },
    {
      label: `Net · ${monthLabel(month)}`,
      value: gbp0(m.net_pennies, { sign: true }),
      cls: m.net_pennies >= 0 ? "pos" : "neg",
      note: m.net_pennies >= 0 ? "in the black" : "spent more than earned",
    },
    {
      label: "Savings rate",
      value: pct(m.savings_rate),
      cls: (m.savings_rate || 0) >= 0 ? "pos" : "neg",
      note: "of income kept",
    },
    {
      label: "Transactions",
      value: String(m.txn_count),
      cls: "",
      note: monthLabel(month),
    },
  ];
  $("#kpi-cards").innerHTML = cards.map((c) => `
    <div class="card">
      <div class="label">${escapeHtml(c.label)}</div>
      <div class="value ${c.cls}">${c.value}</div>
      ${c.note ? `<div class="muted tiny">${escapeHtml(c.note)}</div>` : ""}
    </div>`).join("");
}

function renderCategories(month) {
  const m = DATA.by_month[month] || {};
  $("#category-title").innerHTML =
    `Spend by category <span class="muted tiny">— ${monthLabel(month)}</span>`;
  const big = m.biggest_expense;
  $("#biggest-expense").textContent =
    big ? `Biggest: ${big.name} ${gbpExact(big.spend_pennies)}` : "";
  $("#category-chart").innerHTML = barList(
    (m.categories || []).map((c) => ({ label: c.category, value: c.spend_pennies })),
    { color: "var(--neg)", empty: "No spend recorded this month." },
  );
}

function renderMerchants(month) {
  const m = DATA.by_month[month] || {};
  $("#merchants-chart").innerHTML = barList(
    (m.top_merchants || []).map((x) => ({
      label: x.name, value: x.total_pennies, sub: x.count > 1 ? `×${x.count}` : "",
    })),
    { color: "var(--neg)", empty: "Nothing this month." },
  );
}

function renderIncome(month) {
  const m = DATA.by_month[month] || {};
  $("#income-chart").innerHTML = barList(
    (m.income_sources || []).map((x) => ({
      label: x.name, value: x.total_pennies, sub: x.count > 1 ? `×${x.count}` : "",
    })),
    { color: "var(--pos)", empty: "No income this month." },
  );
}

function renderMonth(month) {
  SELECTED = month;
  renderKpis(month);
  renderCategories(month);
  renderMerchants(month);
  renderIncome(month);
}

// ---- global panels --------------------------------------------------------

function renderAccounts() {
  $("#accounts").innerHTML = DATA.accounts.map((a) => {
    const typeBadge = `<span class="badge">${escapeHtml(a.type)}</span>`;
    // Valuation snapshots (e.g. the ISA) carry an "as of" date; show it so the
    // figure isn't mistaken for a live balance.
    const asOf = a.as_of ? ` <span class="muted tiny">as of ${escapeHtml(a.as_of)}</span>` : "";
    const right = a.connected
      ? `<span class="acct-bal ${a.balance_pennies >= 0 ? "pos" : "neg"}">${gbpExact(a.balance_pennies)}</span>${asOf}`
      : `<span class="pill muted-pill">not connected</span>`;
    return `
      <div class="acct-row ${a.connected ? "" : "dim"}">
        <span class="acct-name">${escapeHtml(a.name)} ${typeBadge}</span>
        ${right}
      </div>`;
  }).join("");
}

function renderCashflow() {
  const months = DATA.monthly_series;
  const el = $("#cashflow-chart");
  if (!months.length) { el.innerHTML = `<p class="empty">No data yet.</p>`; return; }
  const max = Math.max(1, ...months.flatMap((m) => [m.income_pennies, Math.abs(m.spend_pennies)]));
  const w = (v) => (Math.abs(v) / max) * 100;

  const legend = `
    <div class="legend">
      <span><span class="swatch" style="background:var(--pos)"></span>In</span>
      <span><span class="swatch" style="background:var(--neg)"></span>Out</span>
      <span class="muted">net at right · tap a month below to filter</span>
    </div>`;

  const rows = months.map((m) => `
    <button class="month-row ${m.month === SELECTED ? "active" : ""}" data-month="${m.month}">
      <span class="m">${monthLabel(m.month)}</span>
      <div class="month-bars">
        <div class="minibar in"  style="width:${w(m.income_pennies)}%" title="In ${gbpExact(m.income_pennies)}"></div>
        <div class="minibar out" style="width:${w(m.spend_pennies)}%" title="Out ${gbpExact(m.spend_pennies)}"></div>
      </div>
      <span class="net ${m.net_pennies >= 0 ? "pos" : "neg"}">${gbp0(m.net_pennies, { sign: true })}</span>
    </button>`).join("");

  el.innerHTML = legend + `<div class="month-grid">${rows}</div>`;
  el.querySelectorAll(".month-row").forEach((btn) =>
    btn.addEventListener("click", () => selectMonth(btn.dataset.month)));
}

function renderBudgets() {
  const el = $("#budgets");
  if (!DATA.budgets.length) {
    el.innerHTML = `<p class="empty">No budgets set yet. Once you define monthly
      limits per category, you'll see progress bars showing spent vs budget here.</p>`;
    return;
  }
  el.innerHTML = DATA.budgets.map((b) => {
    const frac = b.budget_pennies ? b.spent_pennies / b.budget_pennies : 0;
    const over = frac > 1;
    return `
      <div class="bar-row">
        <span class="name">${escapeHtml(b.category)}</span>
        <span class="track"><span class="fill" style="width:${Math.min(100, frac * 100)}%;background:${over ? "var(--neg)" : "var(--accent)"}"></span></span>
        <span class="amt">${gbpExact(b.spent_pennies)} / ${gbpExact(b.budget_pennies)}</span>
      </div>`;
  }).join("");
}

function renderSubscriptions() {
  const subs = DATA.subscriptions;
  const el = $("#subscriptions");
  if (!subs.length) { el.innerHTML = `<p class="empty">No recurring payments detected yet.</p>`; return; }
  const monthlyTotal = subs.reduce((s, x) => s + x.typical_pennies, 0);
  const rows = subs.map((s) => `
    <div class="sub-row">
      <span class="sub-name">${escapeHtml(s.name)}</span>
      <span class="muted tiny">${s.months_seen} mo · last ${escapeHtml(s.last_seen)}</span>
      <span class="sub-amt">${gbpExact(s.typical_pennies)}</span>
    </div>`).join("");
  el.innerHTML =
    `<p class="muted tiny">~${gbpExact(monthlyTotal)} across ${subs.length} recurring payees (typical amounts).</p>`
    + `<div class="sub-list">${rows}</div>`;
}

function renderLedger() {
  const el = $("#ledger");
  const rows = DATA.ledger;
  if (!rows.length) { el.innerHTML = `<p class="empty">No housemate contributions configured.</p>`; return; }
  const body = rows.map((r) => {
    const bal = r.balance_pennies;
    let pill;
    if (bal > 0) pill = `<span class="pill owes">owes ${gbpExact(bal)}</span>`;
    else if (bal < 0) pill = `<span class="pill credit">+${gbpExact(-bal)} ahead</span>`;
    else pill = `<span class="pill settled">settled</span>`;
    return `
      <tr>
        <td>${escapeHtml(r.housemate)}</td>
        <td>${escapeHtml(r.bill_group)}</td>
        <td class="num">${gbpExact(r.expected_pennies)}</td>
        <td class="num">${gbpExact(r.received_pennies)}</td>
        <td>${pill}</td>
      </tr>`;
  }).join("");
  el.innerHTML = `
    <table class="ledger">
      <thead><tr><th>Housemate</th><th>For</th><th>Expected</th><th>Received</th><th>Status</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function renderRecent() {
  const el = $("#recent");
  if (!DATA.recent.length) { el.innerHTML = `<p class="empty">No transactions.</p>`; return; }
  el.innerHTML = DATA.recent.map((t) => {
    const inflow = t.amount_pennies > 0;
    return `
      <div class="feed-row">
        <span class="feed-date">${escapeHtml(t.date.slice(5))}</span>
        <span class="feed-name">${escapeHtml(t.name)}
          <span class="chip">${escapeHtml(t.category)}</span></span>
        <span class="feed-amt ${inflow ? "pos" : "neg"}">${gbp0(t.amount_pennies, { sign: true })}</span>
      </div>`;
  }).join("");
}

function renderFooter() {
  const accts = DATA.accounts.map((a) => escapeHtml(a.name)).join(" · ");
  const when = new Date(DATA.generated_at).toLocaleString("en-GB",
    { dateStyle: "medium", timeStyle: "short" });
  $("#sources").textContent = `Sources: ${accts}`;
  $("#freshness").textContent = `Updated ${when}`;
}

// ---- orchestration --------------------------------------------------------

function selectMonth(month) {
  if (!DATA.by_month[month]) return;
  $("#month-picker").value = month;
  renderMonth(month);
  // reflect active state on the cashflow rows
  document.querySelectorAll("#cashflow-chart .month-row").forEach((b) =>
    b.classList.toggle("active", b.dataset.month === month));
}

function render(data) {
  DATA = data;
  SELECTED = data.selected_month;

  const picker = $("#month-picker");
  picker.innerHTML = data.months.slice().reverse()
    .map((m) => `<option value="${m}">${monthLabel(m)}</option>`).join("");
  picker.value = SELECTED;
  picker.addEventListener("change", () => selectMonth(picker.value));

  // global panels
  renderAccounts();
  renderCashflow();
  renderBudgets();
  renderSubscriptions();
  renderLedger();
  renderRecent();
  renderFooter();
  // month panels
  renderMonth(SELECTED);

  $("#gate").hidden = true;
  $("#dashboard").hidden = false;
}

// ---- wiring ---------------------------------------------------------------

let envelopePromise = null;
function loadEnvelope() {
  if (!envelopePromise) {
    envelopePromise = fetch(DATA_URL, { cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error(`Could not load data (${r.status}). Has it been exported yet?`);
      return r.json();
    });
  }
  return envelopePromise;
}

function showError(msg) {
  const e = $("#gate-error");
  e.textContent = msg;
  e.hidden = false;
}

async function onUnlock(event) {
  event.preventDefault();
  const btn = $("#unlock-btn");
  const passphrase = $("#passphrase").value;
  $("#gate-error").hidden = true;
  btn.disabled = true;
  btn.textContent = "Decrypting…";
  try {
    const envelope = await loadEnvelope();
    const data = await decryptPayload(envelope, passphrase);
    render(data);
  } catch (err) {
    if (err && err.name === "OperationError") {
      showError("Wrong passphrase — try again.");
    } else {
      showError(err.message || "Could not decrypt.");
    }
    btn.disabled = false;
    btn.textContent = "Unlock";
    $("#passphrase").select();
  }
}

$("#unlock-form").addEventListener("submit", onUnlock);
$("#lock-btn").addEventListener("click", () => location.reload());
loadEnvelope().catch(() => { /* surfaced on unlock attempt */ });
