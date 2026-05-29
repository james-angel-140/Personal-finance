"use strict";

/* Encrypted-static dashboard.
 *
 * Flow: fetch data.enc.json (an AES-256-GCM envelope produced by
 * src/export_dashboard.py) -> derive a key from the passphrase with PBKDF2
 * -> decrypt in-browser -> render. No data is readable without the passphrase,
 * and nothing ever leaves the device. Crypto parameters mirror the exporter.
 */

const DATA_URL = "data.enc.json";

const $ = (sel) => document.querySelector(sel);
const b64ToBytes = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

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

const gbp = (pennies, { sign = false } = {}) => {
  const pounds = pennies / 100;
  const s = new Intl.NumberFormat("en-GB", {
    style: "currency", currency: "GBP", maximumFractionDigits: 0,
  }).format(Math.abs(pounds) >= 1000 ? pounds : pounds); // keep 0dp; pennies shown only where needed
  return sign && pennies > 0 ? "+" + s : s;
};

const gbpExact = (pennies) =>
  new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" })
    .format(pennies / 100);

const monthLabel = (ym) => {
  const [y, m] = ym.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1))
    .toLocaleString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
};

// ---- rendering ------------------------------------------------------------

function renderHeadline(data) {
  const h = data.headline;
  const cards = [
    { label: `Income · ${monthLabel(h.month)}`, value: h.income_pennies, cls: "pos" },
    { label: "Spend", value: h.spend_pennies, cls: "neg" },
    { label: "Net", value: h.net_pennies, cls: h.net_pennies >= 0 ? "pos" : "neg" },
  ];
  $("#headline-cards").innerHTML = cards.map((c) => `
    <div class="card">
      <div class="label">${c.label}</div>
      <div class="value ${c.cls}">${gbpExact(c.value)}</div>
    </div>`).join("");
}

function renderCashflow(data) {
  const months = data.monthly;
  const el = $("#cashflow-chart");
  if (!months.length) { el.innerHTML = `<p class="empty">No data yet.</p>`; return; }

  // Scale bars to the largest single in/out magnitude across all months.
  const max = Math.max(1, ...months.flatMap((m) =>
    [m.income_pennies, Math.abs(m.spend_pennies)]));
  const pct = (v) => (Math.abs(v) / max) * 100;

  const legend = `
    <div class="legend">
      <span><span class="swatch" style="background:var(--pos)"></span>In</span>
      <span><span class="swatch" style="background:var(--neg)"></span>Out</span>
      <span class="muted">net at right</span>
    </div>`;

  const rows = months.map((m) => `
    <div class="month-row">
      <span class="m">${monthLabel(m.month)}</span>
      <div class="month-bars">
        <div class="minibar in"  style="width:${pct(m.income_pennies)}%"
             title="In ${gbpExact(m.income_pennies)}"></div>
        <div class="minibar out" style="width:${pct(m.spend_pennies)}%"
             title="Out ${gbpExact(m.spend_pennies)}"></div>
      </div>
      <span class="net ${m.net_pennies >= 0 ? "value pos" : "value neg"}">${gbp(m.net_pennies, { sign: true })}</span>
    </div>`).join("");

  el.innerHTML = legend + `<div class="month-grid">${rows}</div>`;
}

function renderCategories(data) {
  $("#category-title").innerHTML =
    `Spend by category <span class="muted tiny">— ${monthLabel(data.headline.month)}</span>`;
  const el = $("#category-chart");
  const cats = data.categories;
  if (!cats.length) { el.innerHTML = `<p class="empty">No spend recorded this month.</p>`; return; }

  const max = Math.max(1, ...cats.map((c) => c.spend_pennies));
  el.innerHTML = cats.map((c) => `
    <div class="bar-row">
      <span class="name" title="${escapeHtml(c.category)}">${escapeHtml(c.category)}</span>
      <span class="track"><span class="fill" style="width:${(c.spend_pennies / max) * 100}%;background:var(--neg)"></span></span>
      <span class="amt">${gbpExact(c.spend_pennies)}</span>
    </div>`).join("");
}

function renderLedger(data) {
  const el = $("#ledger");
  const rows = data.ledger;
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
      <thead><tr>
        <th>Housemate</th><th>For</th><th>Expected</th><th>Received</th><th>Status</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function renderFooter(data) {
  const accts = data.accounts.map((a) => escapeHtml(a.name)).join(" · ");
  const when = new Date(data.generated_at).toLocaleString("en-GB",
    { dateStyle: "medium", timeStyle: "short" });
  $("#accounts").textContent = `Sources: ${accts}`;
  $("#freshness").textContent = `Updated ${when}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function render(data) {
  renderHeadline(data);
  renderCashflow(data);
  renderCategories(data);
  renderLedger(data);
  renderFooter(data);
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
    // A wrong passphrase surfaces as a GCM authentication failure (OperationError).
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
// Warm the network fetch so unlocking feels instant.
loadEnvelope().catch(() => { /* surfaced on unlock attempt */ });
