// Gateways panel — Level B orchestration roster: one card per gateway (connected +
// seen bands), an "unserved" callout, and manual override controls (connect / release /
// pin) plus the managed <-> opportunistic mode toggle in the panel header.
//
// Renders entirely off `frame.gateways` / `frame.unserved` (StateFrame, core/models.py).
// When `frame.gateways` is empty (orchestration off / single-gateway runs) the whole
// panel is hidden so those sessions look exactly as before this feature existed.

import { post } from "../ws.js";

const el = (id) => document.getElementById(id);

// gw id -> { root, ...refs, connectedRows: Map<dev,node>, seenRows: Map<dev,node> }
const gatewayNodes = new Map();

// The frame doesn't surface manual-pin state per device (Task 7's GatewayState /
// ConnectedBand carry no `pinned` field), so the pin toggle's *label* is tracked
// client-side for this session only — it reflects "have I pinned this dev from this
// tab" rather than authoritative backend state.
const pinnedLocally = new Set();

// The frame doesn't surface a single global orchestrator mode either (each gateway
// reports its own `mode`, which can briefly lag the broadcast control message). We
// infer a starting value from the majority of reported gateway modes, then track the
// operator's own toggles locally from there.
let globalMode = null;

// Gateway display aliases are an Ops-UI-only convenience so an operator can
// tell which physical gateway is which ("Living Room" vs esp32-01-a172e0).
// They live in localStorage keyed by gateway id -- never sent to the backend
// or persisted on the gateway, so they cost nothing on the wire and the
// canonical id stays the source of truth (shown on hover).
const ALIAS_KEY = "cuddle.gwAliases";

function loadAliases() {
  try {
    return JSON.parse(localStorage.getItem(ALIAS_KEY)) || {};
  } catch {
    return {};
  }
}

function saveAlias(gwId, alias) {
  const aliases = loadAliases();
  if (alias) aliases[gwId] = alias;
  else delete aliases[gwId]; // empty input clears the alias -> revert to id
  localStorage.setItem(ALIAS_KEY, JSON.stringify(aliases));
}

function gwDisplayName(gwId) {
  return loadAliases()[gwId] || gwId;
}

// Swap the gateway's name span for an input to rename it in place. Enter or
// blur commits (empty clears), Escape cancels; the label then reverts to the
// alias-or-id and keeps the canonical id on hover.
function startRename(node) {
  if (node.id.parentNode == null) return; // already editing / detached
  const input = document.createElement("input");
  input.className = "gwrenameinput";
  input.value = loadAliases()[node.gwId] || "";
  input.placeholder = node.gwId;
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    if (save) saveAlias(node.gwId, input.value.trim());
    node.id.textContent = gwDisplayName(node.gwId);
    node.id.title = node.gwId;
    input.replaceWith(node.id);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  node.id.replaceWith(input);
  input.focus();
  input.select();
}

function inferMode(gateways) {
  if (!gateways.length) return "managed";
  const managed = gateways.filter((g) => g.mode === "managed").length;
  return managed >= gateways.length / 2 ? "managed" : "opportunistic";
}

function syncSelect(sel, opts) {
  if (document.activeElement === sel) return;
  const sig = opts.map((o) => `${o.value}:${o.label}`).join("|");
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.innerHTML = "";
  for (const o of opts) {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    sel.appendChild(opt);
  }
}

function bandLabel(dev, personId, people) {
  if (personId) {
    const p = people.find((p) => p.person_id === personId);
    if (p) return p.display_name;
  }
  return dev;
}

const REASON_LABEL = {
  no_capacity: "no capacity",
  waiting_to_advertise: "waiting to advertise",
};

// ---- panel header: mode toggle ----------------------------------------------

function initModeToggle() {
  el("orchModeToggle").addEventListener("click", async () => {
    const next = globalMode === "managed" ? "opportunistic" : "managed";
    await post("/api/orchestrator/mode", { mode: next });
    globalMode = next;
    el("orchModeToggle").textContent = globalMode;
  });
}

// ---- unserved callout --------------------------------------------------------

function renderUnserved(unserved) {
  const callout = el("unservedCallout");
  const list = el("unservedList");
  callout.style.display = unserved.length ? "" : "none";
  list.innerHTML = "";
  for (const u of unserved) {
    const row = document.createElement("div");
    row.className = "unservedrow";
    row.innerHTML = `
      <span class="banddev"></span>
      <span class="bandrssi"></span>
      <span class="unservedreason"></span>`;
    row.querySelector(".banddev").textContent = u.dev;
    row.querySelector(".bandrssi").textContent = u.rssi != null ? `${u.rssi} dBm` : "—";
    row.querySelector(".unservedreason").textContent = REASON_LABEL[u.reason] || u.reason;
    list.appendChild(row);
  }
}

// ---- one connected-band row --------------------------------------------------

function makeConnectedRow(dev) {
  const row = document.createElement("div");
  row.className = "bandrow";
  row.innerHTML = `
    <span class="banddev"></span>
    <span class="bandrssi"></span>
    <span class="grow"></span>
    <button class="pinbtn" title="pin this band to its current gateway (prevents rebalance)">pin</button>
    <button class="releasebtn" title="release this band back to the pool">Release</button>`;
  const pinbtn = row.querySelector(".pinbtn");
  const releasebtn = row.querySelector(".releasebtn");
  pinbtn.addEventListener("click", async () => {
    const next = !pinnedLocally.has(dev);
    if (next) pinnedLocally.add(dev); else pinnedLocally.delete(dev);
    pinbtn.classList.toggle("active", next);
    pinbtn.textContent = next ? "pinned" : "pin";
    await post("/api/orchestrator/pin", { dev, pinned: next });
  });
  // Two-click confirm, matching the person-card "Remove" button.
  releasebtn.addEventListener("click", async () => {
    if (releasebtn.classList.contains("confirm")) {
      await post("/api/orchestrator/release", { dev });
    } else {
      releasebtn.classList.add("confirm");
      releasebtn.textContent = "Confirm?";
      setTimeout(() => {
        releasebtn.classList.remove("confirm");
        releasebtn.textContent = "Release";
      }, 3000);
    }
  });
  return {
    root: row,
    dev: row.querySelector(".banddev"),
    rssi: row.querySelector(".bandrssi"),
    pinbtn,
    releasebtn,
  };
}

function updateConnectedRow(node, band, people) {
  node.dev.textContent = bandLabel(band.dev, band.person_id, people);
  node.dev.title = band.dev;
  node.rssi.textContent = band.rssi != null ? `${band.rssi} dBm` : "—";
  const isPinned = pinnedLocally.has(band.dev);
  node.pinbtn.classList.toggle("active", isPinned);
  node.pinbtn.textContent = isPinned ? "pinned" : "pin";
}

// ---- one seen-band row (unconnected, manual "connect ->") -------------------

function makeSeenRow(dev) {
  const row = document.createElement("div");
  row.className = "bandrow";
  row.innerHTML = `
    <span class="banddev"></span>
    <span class="bandrssi"></span>
    <span class="grow"></span>
    <select class="connectsel"></select>`;
  const sel = row.querySelector(".connectsel");
  sel.addEventListener("change", async () => {
    const gw = sel.value;
    sel.value = "";
    sel.dataset.sig = "";
    if (gw) await post("/api/orchestrator/connect", { dev, gw });
  });
  return { root: row, dev: row.querySelector(".banddev"), rssi: row.querySelector(".bandrssi"), sel };
}

function updateSeenRow(node, band, gwId, allGatewayIds, people) {
  // Show the enrolled person's name when this MAC is already registered, else
  // the bare MAC (bandLabel handles the fallback). Keep the MAC on hover.
  node.dev.textContent = bandLabel(band.dev, band.person_id, people);
  node.dev.title = band.dev;
  node.rssi.textContent = band.rssi != null ? `${band.rssi} dBm` : "—";
  syncSelect(node.sel, [
    { value: "", label: "connect →" },
    ...allGatewayIds.map((id) => ({ value: id, label: id === gwId ? `${id} (this)` : id })),
  ]);
}

// ---- one gateway card ---------------------------------------------------------

function makeGatewayCard() {
  const root = document.createElement("div");
  root.className = "card gwcard";
  root.innerHTML = `
    <div class="cardhead">
      <span class="dot"></span>
      <span class="name gwid"></span>
      <button class="gwrename" title="rename this gateway (local label only)">✎</button>
      <span class="badge gwmode"></span>
      <span class="grow"></span>
      <span class="gwcap"></span>
    </div>
    <div class="gwsection">
      <div class="gwlabel">connected</div>
      <div class="gwconnected"></div>
      <div class="gwempty gwconnectedempty">none</div>
    </div>
    <div class="gwsection">
      <div class="gwlabel">seen</div>
      <div class="gwseen"></div>
      <div class="gwempty gwseenempty">none</div>
    </div>`;
  return {
    root,
    dot: root.querySelector(".dot"),
    id: root.querySelector(".gwid"),
    rename: root.querySelector(".gwrename"),
    mode: root.querySelector(".gwmode"),
    cap: root.querySelector(".gwcap"),
    connectedWrap: root.querySelector(".gwconnected"),
    connectedEmpty: root.querySelector(".gwconnectedempty"),
    seenWrap: root.querySelector(".gwseen"),
    seenEmpty: root.querySelector(".gwseenempty"),
    connectedRows: new Map(),
    seenRows: new Map(),
  };
}

function reconcileRows(wrap, emptyNode, rows, bands, makeRow, updateRow) {
  const seen = new Set();
  for (const b of bands) {
    seen.add(b.dev);
    let node = rows.get(b.dev);
    if (!node) {
      node = makeRow(b.dev);
      rows.set(b.dev, node);
      wrap.appendChild(node.root);
    }
    updateRow(node, b);
  }
  for (const [dev, node] of rows) {
    if (!seen.has(dev)) {
      node.root.remove();
      rows.delete(dev);
    }
  }
  emptyNode.style.display = bands.length ? "none" : "";
}

// ---- top-level render ---------------------------------------------------------

export function initGateways() {
  initModeToggle();
}

export function renderGateways(frame) {
  const gateways = frame.gateways || [];
  const unserved = frame.unserved || [];
  const people = frame.people || [];
  const panel = el("gatewaysPanel");

  panel.style.display = gateways.length ? "" : "none";
  if (!gateways.length) return;

  if (globalMode === null) {
    globalMode = inferMode(gateways);
    el("orchModeToggle").textContent = globalMode;
  }

  renderUnserved(unserved);

  const allGatewayIds = gateways.map((g) => g.id);
  const wrap = el("gatewayCards");
  const seenIds = new Set();
  for (const gw of gateways) {
    seenIds.add(gw.id);
    let node = gatewayNodes.get(gw.id);
    if (!node) {
      node = makeGatewayCard();
      node.gwId = gw.id;
      node.rename.addEventListener("click", () => startRename(node));
      node.id.addEventListener("click", () => startRename(node));
      gatewayNodes.set(gw.id, node);
      wrap.appendChild(node.root);
    }
    // Show the operator's local alias if set, else the canonical id; keep the
    // real id on hover. Skipped while a rename input is active (id detached).
    if (node.id.parentNode) {
      node.id.textContent = gwDisplayName(gw.id);
      node.id.title = gw.id;
    }
    node.dot.style.background = gw.online ? "var(--good)" : "var(--bad)";
    node.dot.title = gw.online ? "online" : "offline";
    node.mode.textContent = gw.mode;
    node.mode.classList.toggle("managed", gw.mode === "managed");
    node.cap.textContent = `${gw.connected.length}/${gw.capacity}`;

    reconcileRows(
      node.connectedWrap, node.connectedEmpty, node.connectedRows, gw.connected,
      makeConnectedRow, (n, b) => updateConnectedRow(n, b, people),
    );
    reconcileRows(
      node.seenWrap, node.seenEmpty, node.seenRows, gw.seen,
      makeSeenRow, (n, b) => updateSeenRow(n, b, gw.id, allGatewayIds, people),
    );
  }
  for (const [id, node] of gatewayNodes) {
    if (!seenIds.has(id)) {
      node.root.remove();
      gatewayNodes.delete(id);
    }
  }
}
