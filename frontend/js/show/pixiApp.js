// PixiJS bootstrap + preset switcher (style-vs-renderer).
//
// One WebGL Application, one canvas. A "preset" is a STYLE (settings) bound to a RENDERER
// (engine). Presets live in a localStorage LIBRARY, seeded from registry.PRESETS: you
// OPEN one from a dialog and SAVE your edits back into it (Save As makes a new one).
// Edits also auto-commit on switch / page unload so nothing is lost. Every renderer reads
// the same StateFrame from the store.

import { Application } from "../../vendor/pixi.min.mjs";
import { getFrame } from "../store.js";
import { PRESETS, RENDERERS } from "../presets/registry.js";
import { FILTERS, FILTER_ORDER } from "../presets/filters.js";
import { SYSTEM_PARAMS, newParticleSystem } from "../presets/particles.js";
import { REACTION_TYPES, LOCATIONS, TRIGGERS, makeReaction } from "../presets/events.js";

const CSS = `
#preset-open { position: fixed; top: 14px; left: 14px; z-index: 20; font: 12px system-ui, sans-serif;
  background: rgba(30,16,24,0.82); color: #f2e4de; border: 1px solid rgba(255,255,255,0.16);
  border-radius: 7px; padding: 7px 12px; cursor: pointer; backdrop-filter: blur(6px); }
#preset-open:hover { border-color: rgba(255,255,255,0.4); }
#preset-open .cur { color: #e8663f; font-weight: 600; margin-left: 6px; }
#preset-dialog { position: fixed; inset: 0; z-index: 30; display: none; background: rgba(0,0,0,0.45); }
#preset-dialog.open { display: block; }
#preset-dialog .box { position: absolute; top: 56px; left: 14px; width: 300px; max-height: 72vh; overflow-y: auto;
  background: rgba(20,10,16,0.97); border: 1px solid rgba(255,255,255,0.14); border-radius: 10px;
  padding: 12px; font: 12px system-ui, sans-serif; color: #f2e4de; backdrop-filter: blur(8px); }
#preset-dialog h3 { margin: 0 0 10px; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: #b89; }
#preset-dialog .item { display: flex; align-items: center; gap: 6px; padding: 7px 8px; border-radius: 7px; cursor: pointer; }
#preset-dialog .item:hover { background: rgba(255,255,255,0.06); }
#preset-dialog .item.on { background: #e8663f; color: #150a10; font-weight: 600; }
#preset-dialog .item .lbl { flex: 1; }
#preset-dialog .item .ren { opacity: .5; font-size: 10px; }
#preset-dialog .item .del { opacity: .5; padding: 0 4px; }
#preset-dialog .item .del:hover { opacity: 1; color: #e0245e; }
#preset-dialog .foot { display: flex; gap: 6px; margin-top: 10px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.08); }
#preset-dialog .foot button { background: #2e1622; color: #f2e4de; border: 1px solid rgba(255,255,255,0.14);
  border-radius: 6px; padding: 5px 9px; cursor: pointer; font: 11px system-ui; }
#preset-dialog .foot button:hover { border-color: rgba(255,255,255,0.4); }
#preset-ctrl { position: fixed; top: 56px; left: 14px; width: 232px; z-index: 20;
  max-height: calc(100vh - 72px); overflow-y: auto;
  background: rgba(20,10,16,0.86); border: 1px solid rgba(255,255,255,0.1); border-radius: 9px;
  padding: 10px 12px; font: 11px system-ui, sans-serif; color: #f2e4de; backdrop-filter: blur(6px); }
#preset-ctrl h3 { margin: 0 0 8px; font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: #b89; }
#preset-ctrl .grp { margin: 11px 0 3px; font-size: 9px; letter-spacing: .12em; text-transform: uppercase;
  color: #e8663f; border-top: 1px solid rgba(255,255,255,0.07); padding-top: 7px; }
#preset-ctrl .grp:first-of-type { border-top: none; padding-top: 0; margin-top: 4px; }
#preset-ctrl .r { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
#preset-ctrl label { flex: 0 0 70px; color: #c9b; cursor: help; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#preset-ctrl input[type=range] { flex: 1; min-width: 0; }
#preset-ctrl input[type=color] { flex: 1; height: 18px; padding: 0; border: 1px solid rgba(255,255,255,0.14);
  border-radius: 4px; background: none; cursor: pointer; }
#preset-ctrl select { flex: 1; background: #241019; color: #f2e4de; border: 1px solid rgba(255,255,255,0.14);
  border-radius: 5px; padding: 2px 3px; font: 11px system-ui; }
#preset-ctrl input[type=checkbox] { margin: 0 auto 0 0; cursor: pointer; }
#preset-ctrl .v { flex: 0 0 34px; text-align: right; font-variant-numeric: tabular-nums; color: #fff; }
#preset-ctrl .fhdr { display: flex; align-items: center; gap: 6px; margin: 6px 0 2px; }
#preset-ctrl .fhdr input[type=checkbox] { margin: 0; cursor: pointer; }
#preset-ctrl .fhdr .fname { flex: 1; color: #f2e4de; font-weight: 600; }
#preset-ctrl .fhdr .fname.off { color: #8a7580; font-weight: 400; }
#preset-ctrl .fhdr .mv { cursor: pointer; opacity: .45; padding: 0 2px; font-size: 12px; user-select: none; }
#preset-ctrl .fhdr .mv:hover { opacity: 1; }
#preset-ctrl .fp { padding-left: 10px; border-left: 2px solid rgba(232,102,63,0.3); margin-left: 2px; }
#preset-ctrl .fp label { flex: 0 0 60px; }
#preset-ctrl .fhdr .ren { opacity: .5; font-size: 10px; }
#preset-ctrl .fhdr .add { cursor: pointer; opacity: .55; font-size: 13px; padding: 0 2px; }
#preset-ctrl .fhdr .add:hover { opacity: 1; }
#preset-ctrl .rxn { margin: 3px 0 5px 8px; padding: 4px 6px; border-left: 2px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.03); border-radius: 0 5px 5px 0; position: relative; }
#preset-ctrl .rxn .rr { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
#preset-ctrl .rxn .rr span { flex: 0 0 30px; color: #a89; }
#preset-ctrl .rxn .rr select { flex: 1; background: #241019; color: #f2e4de;
  border: 1px solid rgba(255,255,255,0.14); border-radius: 4px; padding: 1px 3px; font: 10px system-ui; }
#preset-ctrl .rxn .del { position: absolute; top: 3px; right: 4px; cursor: pointer; opacity: .5; font-size: 11px; }
#preset-ctrl .rxn .del:hover { opacity: 1; color: #e0245e; }
#preset-ctrl .rxn.off { opacity: .45; }
#preset-ctrl .rxn-set { font-size: 9px; letter-spacing: .04em; text-transform: uppercase; color: #a89;
  margin: 5px 0 2px; opacity: .7; }
#preset-ctrl .rxn .actchk { flex: 0 0 auto; margin: 0; cursor: pointer; }
#preset-ctrl .sbar { display: flex; gap: 6px; align-items: center; margin-top: 10px;
  padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.08); flex-wrap: wrap; }
#preset-ctrl .sbar button { background: #2e1622; color: #f2e4de; border: 1px solid rgba(255,255,255,0.14);
  border-radius: 6px; padding: 4px 8px; cursor: pointer; font: 10px system-ui; }
#preset-ctrl .sbar button:hover { border-color: rgba(255,255,255,0.4); }
#preset-ctrl .sbar .note { color: #7cb; font-size: 10px; }
#preset-ctrl .sbar.top { margin-top: 6px; margin-bottom: 4px; padding-top: 0; padding-bottom: 9px;
  border-top: none; border-bottom: 1px solid rgba(255,255,255,0.1); }
#preset-ctrl .grp .add { cursor: pointer; opacity: .7; font-size: 13px; margin-left: 5px;
  text-transform: none; color: #f2e4de; user-select: none; }
#preset-ctrl .grp .add:hover { opacity: 1; }
#preset-ctrl .fhdr .fname.ren { cursor: text; }
#preset-ctrl .fhdr .fname.ren:hover { text-decoration: underline dotted; }
#preset-ctrl .fhdr .del:hover { color: #e0245e; }
#preset-ctrl .fhdr .tsel { flex: 0 0 auto; width: auto; font-size: 10px; padding: 1px 4px; }
#preset-ctrl input[type=text], #preset-ctrl textarea { flex: 1; min-width: 0; background: #241019;
  color: #f2e4de; border: 1px solid rgba(255,255,255,0.14); border-radius: 5px; padding: 2px 4px;
  font: 10px ui-monospace, Menlo, monospace; }
#preset-ctrl .r .src { flex: 0 0 auto; font-size: 8px; text-transform: uppercase; letter-spacing: .05em;
  color: #9a8590; padding: 2px 5px; border: 1px solid rgba(255,255,255,0.14); border-radius: 8px;
  white-space: nowrap; }
#preset-ctrl .r .src.on { color: #7ad7c7; border-color: rgba(122,215,199,0.5); background: rgba(122,215,199,0.08); }
#preset-ctrl .r .clr { flex: 0 0 auto; cursor: pointer; color: #8a7580; font-size: 11px; padding: 0 3px; user-select: none; }
#preset-ctrl .r .clr:hover { color: #e0245e; }
#preset-ctrl .r.ta { align-items: flex-start; }
#preset-ctrl textarea { resize: vertical; line-height: 1.35; }
`;

export async function startPixiApp({ mount }) {
  const app = new Application();
  await app.init({ background: "#150a10", antialias: true, resolution: window.devicePixelRatio || 1, autoDensity: true, resizeTo: mount });
  mount.appendChild(app.canvas);

  let current = null, currentId = null;
  const defaultStates = {};    // id -> pristine state for Reset
  const rendererDefaults = {}; // renderer name -> its fresh state (captured once, before any tuning)

  const style = document.createElement("style"); style.textContent = CSS; document.head.appendChild(style);
  const openBtn = document.createElement("div"); openBtn.id = "preset-open"; document.body.appendChild(openBtn);
  const dialog = document.createElement("div"); dialog.id = "preset-dialog"; document.body.appendChild(dialog);
  const ctrlPanel = document.createElement("div"); ctrlPanel.id = "preset-ctrl"; document.body.appendChild(ctrlPanel);

  // ---- library (localStorage), seeded from the built-in presets ----
  const LIB_KEY = "cuddle.preset.library", LAST_KEY = "cuddle.preset.last";
  let library;
  try { library = JSON.parse(localStorage.getItem(LIB_KEY)); } catch { library = null; }
  if (!Array.isArray(library) || !library.length) {
    library = PRESETS.map((p) => ({ id: p.id, label: p.label, renderer: p.renderer, state: p.state || null }));
  }
  const persistLibrary = () => { try { localStorage.setItem(LIB_KEY, JSON.stringify(library)); } catch {} };
  const libEntry = (id) => library.find((p) => p.id === id);
  function commit() { const e = libEntry(currentId); if (e && current?.getState) { e.state = current.getState(); persistLibrary(); } }

  // ---- Open button + dialog ----
  function refreshOpenBtn() { const e = libEntry(currentId); openBtn.innerHTML = `Open Preset<span class="cur">${e ? e.label : "—"}</span>`; }
  openBtn.onclick = () => { buildDialog(); dialog.classList.add("open"); };
  dialog.onclick = (ev) => { if (ev.target === dialog) dialog.classList.remove("open"); };
  function buildDialog() {
    dialog.innerHTML = "";
    const box = document.createElement("div"); box.className = "box";
    box.innerHTML = `<h3>Open Preset</h3>`;
    library.forEach((p) => {
      const it = document.createElement("div");
      it.className = "item" + (p.id === currentId ? " on" : "");
      const builtin = PRESETS.some((b) => b.id === p.id);
      it.innerHTML = `<span class="lbl">${p.label}</span><span class="ren">${p.renderer}</span>` + (builtin ? "" : `<span class="del" title="delete">✕</span>`);
      it.querySelector(".lbl").onclick = () => { select(p.id); dialog.classList.remove("open"); };
      it.querySelector(".ren").onclick = () => { select(p.id); dialog.classList.remove("open"); };
      const del = it.querySelector(".del");
      if (del) del.onclick = (e) => { e.stopPropagation(); library = library.filter((x) => x.id !== p.id); persistLibrary(); buildDialog(); };
      box.appendChild(it);
    });
    const foot = document.createElement("div"); foot.className = "foot";
    foot.innerHTML = `<button data-a="import">Import file…</button><button data-a="close">Close</button>`;
    foot.querySelector('[data-a="import"]').onclick = importPreset;
    foot.querySelector('[data-a="close"]').onclick = () => dialog.classList.remove("open");
    box.appendChild(foot);
    dialog.appendChild(box);
  }

  // ---- controls panel ----
  function buildControls() {
    ctrlPanel.innerHTML = "";
    const controls = current?.controls, params = current?.params;
    if (!controls || !controls.length || !params) { ctrlPanel.style.display = "none"; return; }
    ctrlPanel.style.display = "block";
    const e = libEntry(currentId);
    ctrlPanel.innerHTML = `<h3>${e ? e.label : currentId}</h3>`;
    ctrlPanel.appendChild(buildActionBar()); // Save / Save As / Rename / Reset — saves the WHOLE preset
    let lastGroup = null;
    for (const c of controls) {
      if (c.group && c.group !== lastGroup) {
        lastGroup = c.group;
        const g = document.createElement("div"); g.className = "grp"; g.textContent = c.group;
        ctrlPanel.appendChild(g);
      }
      // text controls (node/edge PNG paths) reload textures on edit; others apply live
      ctrlPanel.appendChild(makeControlRow(c, params, "r", c.type === "text" ? () => current.applyTextures?.() : undefined));
    }
    if (Array.isArray(params.filters)) buildFilterEditor(params.filters);
    if (params.particleSystems) buildParticleEditor(params.particleSystems);
    if (Array.isArray(params.events)) buildEventsEditor(params.events, params.particleSystems || {});
  }

  // One control row bound to obj[def.key] — type: range (default) | color | toggle | select.
  // onChange (optional) fires after any edit (e.g. to rebuild particle emitters live).
  function makeControlRow(def, obj, cls = "r", onChange) {
    const row = document.createElement("div"); row.className = cls;
    const val = obj[def.key]; const tip = (def.tip || def.key).replace(/"/g, "&quot;");
    const type = def.type || "range";
    if (type === "color") {
      row.innerHTML = `<label title="${tip}">${def.label}</label><input type="color" value="${val}" title="${tip}">`;
      row.querySelector("input").oninput = (ev) => { obj[def.key] = ev.target.value; onChange?.(); };
    } else if (type === "toggle") {
      row.innerHTML = `<label title="${tip}">${def.label}</label><input type="checkbox" ${val ? "checked" : ""} title="${tip}">`;
      row.querySelector("input").onchange = (ev) => { obj[def.key] = ev.target.checked; onChange?.(); };
    } else if (type === "select") {
      const opts = (def.options || []).map((o) => `<option value="${o}" ${o === val ? "selected" : ""}>${o}</option>`).join("");
      row.innerHTML = `<label title="${tip}">${def.label}</label><select title="${tip}">${opts}</select>`;
      row.querySelector("select").onchange = (ev) => { obj[def.key] = ev.target.value; onChange?.(); };
    } else if (type === "text") {
      const esc = String(val ?? "").replace(/"/g, "&quot;");
      // Source badge: shows the fallback (generated/soft dot/sliders) when blank, or the active
      // source (PNG/file) highlighted when a path is set. The ✕ clears back to that default.
      const srcLabel = (set) => set ? (def.setLabel || "file") : (def.emptyLabel || "default");
      row.innerHTML = `<label title="${tip}">${def.label}</label><input type="text" value="${esc}" placeholder="${def.placeholder || ""}" title="${tip}"><span class="src" title="current source"></span><span class="clr" title="clear — use the generated default">✕</span>`;
      const input = row.querySelector("input"), badge = row.querySelector(".src"), clr = row.querySelector(".clr");
      const sync = () => { const set = !!input.value.trim(); badge.textContent = srcLabel(set); badge.classList.toggle("on", set); clr.style.display = set ? "" : "none"; };
      input.oninput = sync;
      input.onchange = (ev) => { obj[def.key] = ev.target.value.trim(); onChange?.(); };
      clr.onclick = () => { input.value = ""; obj[def.key] = ""; sync(); onChange?.(); };
      sync();
    } else if (type === "textarea") {
      row.className = cls + " ta";
      const esc = String(val ?? "").replace(/</g, "&lt;");
      row.innerHTML = `<label title="${tip}">${def.label}</label><textarea rows="4" spellcheck="false" placeholder="${def.placeholder || ""}" title="${tip}">${esc}</textarea>`;
      row.querySelector("textarea").onchange = (ev) => { obj[def.key] = ev.target.value; onChange?.(); };
    } else {
      row.innerHTML = `<label title="${tip}">${def.label}</label>
        <input type="range" min="${def.min}" max="${def.max}" step="${def.step}" value="${val}" title="${tip}">
        <span class="v" title="${tip}">${(+val).toFixed(def.step < 1 ? 2 : 0)}</span>`;
      const input = row.querySelector("input"), out = row.querySelector(".v");
      input.oninput = () => { obj[def.key] = parseFloat(input.value); out.textContent = (+input.value).toFixed(def.step < 1 ? 2 : 0); onChange?.(); };
    }
    return row;
  }

  // Particle Systems editor — add/rename/delete systems, set a texture path per system, tune
  // friendly params, and (advanced) paste editor JSON that overrides the sliders. Rebuild
  // emitters on any edit. `aura` and `joinBurst` are the renderer's built-ins (not deletable);
  // added systems fire once bound to an event.
  const BUILTIN_SYS = { aura: 1, joinBurst: 1 };
  function buildParticleEditor(systems) {
    const g = document.createElement("div"); g.className = "grp";
    g.innerHTML = `Particle Systems <span class="mv add" title="new system">＋</span>`;
    g.querySelector(".add").onclick = () => {
      const name = (prompt("New particle system name:", "Sparkle") || "").trim();
      if (!name) return;
      let id = name.replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "sys";
      const base = id; let n = 2; while (systems[id]) id = base + "_" + n++;
      systems[id] = newParticleSystem(name);
      current.applyParticles?.(); buildControls();
    };
    ctrlPanel.appendChild(g);
    const onEdit = () => current.applyParticles?.();
    for (const name of Object.keys(systems)) {
      const sys = systems[name];
      const hdr = document.createElement("div"); hdr.className = "fhdr";
      hdr.innerHTML = `<span class="fname ren" title="rename">${sys.label || name}</span>
        <select class="tsel" title="emission type">
          <option value="continuous" ${sys.type === "continuous" ? "selected" : ""}>continuous</option>
          <option value="hit" ${sys.type === "hit" ? "selected" : ""}>hit</option>
        </select>
        ${BUILTIN_SYS[name] ? "" : `<span class="mv del" title="delete system">✕</span>`}`;
      hdr.querySelector(".ren").onclick = () => { const nn = (prompt("Rename system:", sys.label || name) || "").trim(); if (nn) { sys.label = nn; buildControls(); } };
      hdr.querySelector(".tsel").onchange = (ev) => { sys.type = ev.target.value; current.applyParticles?.(); buildControls(); };
      const del = hdr.querySelector(".del");
      if (del) del.onclick = () => { delete systems[name]; current.applyParticles?.(); buildControls(); };
      ctrlPanel.appendChild(hdr);
      ctrlPanel.appendChild(makeControlRow({ key: "texture", label: "PNG", type: "text", placeholder: "/assets/spark.png", emptyLabel: "soft dot", setLabel: "PNG", tip: "Texture path or URL served by the frontend — blank uses the soft dot" }, sys, "r fp", onEdit));
      ctrlPanel.appendChild(makeControlRow({ key: "shape", label: "Shape", type: "select", options: ["scatter", "ring"], tip: "scatter = spray in random directions · ring = particles fly radially outward from the spawn point (an expanding ring ripple)" }, sys, "r fp", onEdit));
      for (const p of SYSTEM_PARAMS) {
        if (p.only && p.only !== sys.type) continue;
        ctrlPanel.appendChild(makeControlRow(p, sys, "r fp", onEdit));
      }
      ctrlPanel.appendChild(makeControlRow({ key: "config", label: "Emitter JSON", type: "text", placeholder: "/assets/emitters/example.json", emptyLabel: "sliders", setLabel: "file", tip: "Path/URL to a Pixi particle-editor JSON file (edit it in the editor, save back down). When set it overrides the sliders above; the PNG + color still apply." }, sys, "r fp", onEdit));
    }
  }

  // Events editor — reactions bound to each event (structure; runtime dispatch is next).
  function buildEventsEditor(events, systems) {
    const g = document.createElement("div"); g.className = "grp"; g.textContent = "Events";
    ctrlPanel.appendChild(g);
    const refOptions = (r) => r.type === "particle" ? Object.keys(systems) : r.type === "filter" ? FILTER_ORDER : ["color", "graphic", "scale", "opacity"];
    events.forEach((ev) => {
      const hdr = document.createElement("div"); hdr.className = "fhdr";
      hdr.innerHTML = `<span class="fname">${ev.label}</span><span class="mv add" title="add reaction">＋</span>`;
      hdr.querySelector(".add").onclick = () => { ev.reactions.push(makeReaction("particle")); buildControls(); };
      ctrlPanel.appendChild(hdr);
      ev.reactions.forEach((r, ri) => {
        const off = r.active === false;
        const box = document.createElement("div"); box.className = "rxn" + (off ? " off" : "");
        const sel = (label, key, options) =>
          `<div class="rr"><span>${label}</span><select data-k="${key}">${options.map((o) => `<option value="${o}" ${o === r[key] ? "selected" : ""}>${o}</option>`).join("")}</select></div>`;
        box.innerHTML = `<div class="rr"><span>on</span><input type="checkbox" class="actchk" ${off ? "" : "checked"} title="enable / disable this reaction"></div>` +
          sel("type", "type", REACTION_TYPES) + sel("ref", "ref", refOptions(r)) +
          sel("loc", "location", LOCATIONS) + sel("trig", "trigger", TRIGGERS) +
          `<span class="mv del" title="remove">✕</span>`;
        box.querySelector(".actchk").onchange = (e) => { r.active = e.target.checked; box.classList.toggle("off", !e.target.checked); };
        box.querySelectorAll("select").forEach((s) => {
          // type OR ref change re-renders so the exposed settings match the new target
          s.onchange = () => { r[s.dataset.k] = s.value; if (s.dataset.k === "type") r.ref = ""; if (s.dataset.k === "type" || s.dataset.k === "ref") buildControls(); };
        });
        box.querySelector(".del").onclick = () => { ev.reactions.splice(ri, 1); buildControls(); };
        ctrlPanel.appendChild(box);
        reactionSettings(r, box, systems); // inline settings for the referenced item
      });
    });
  }

  // Expose the referenced item's settings inline under a reaction. Particle → the SHARED
  // system's params (same object the Particle Systems panel edits). Filter → this instance's
  // filter params (amplitude/…) + Duration, stored on the reaction. Property → Amount + Duration.
  function reactionSettings(r, box, systems) {
    if (!r.ref) return;
    const note = () => { const d = document.createElement("div"); d.className = "rxn-set"; return d; };
    if (r.type === "particle") {
      const sys = systems[r.ref]; if (!sys) return;
      const wrap = note(); wrap.textContent = "system settings (shared):"; box.appendChild(wrap);
      const onEdit = () => current.applyParticles?.();
      box.appendChild(makeControlRow({ key: "shape", label: "Shape", type: "select", options: ["scatter", "ring"], tip: "scatter = random directions · ring = radial ripple" }, sys, "r fp", onEdit));
      for (const p of SYSTEM_PARAMS) { if (p.only && p.only !== sys.type) continue; box.appendChild(makeControlRow(p, sys, "r fp", onEdit)); }
    } else if (r.type === "filter") {
      const def = FILTERS[r.ref]; if (!def) return;
      r.params = r.params || {};
      for (const p of def.params) if (r.params[p.key] === undefined) r.params[p.key] = p.def; // seed (incl. center, hidden)
      if (r.params.dur === undefined) r.params.dur = def.fx?.dur ?? 0.6;
      const wrap = note(); wrap.textContent = "filter settings:"; box.appendChild(wrap);
      for (const p of def.params) { if (p.key === "cx" || p.key === "cy") continue; box.appendChild(makeControlRow(p, r.params, "r fp")); } // center comes from location
      box.appendChild(makeControlRow({ key: "dur", label: "Duration", min: 0.1, max: 2.5, step: 0.05, tip: "How long the ripple animates (s)" }, r.params, "r fp"));
    } else if (r.type === "property") {
      r.params = r.params || {};
      if (r.params.amount === undefined) r.params.amount = r.ref === "opacity" ? 0.6 : 0.5;
      if (r.params.dur === undefined) r.params.dur = 0.4;
      const wrap = note(); wrap.textContent = "property settings:"; box.appendChild(wrap);
      if (r.ref !== "color") box.appendChild(makeControlRow({ key: "amount", label: "Amount", min: 0, max: 1.5, step: 0.05, tip: "Strength of the pop/dip (× base)" }, r.params, "r fp"));
      box.appendChild(makeControlRow({ key: "dur", label: "Duration", min: 0.05, max: 1.5, step: 0.05, tip: "Hit reactions fade over this many seconds" }, r.params, "r fp"));
    }
  }

  // The filter stack: each filter has a toggle + reorder, and its params when active.
  function buildFilterEditor(list) {
    const g = document.createElement("div"); g.className = "grp"; g.textContent = "Filter Stack";
    ctrlPanel.appendChild(g);
    list.forEach((f, idx) => {
      const def = FILTERS[f.type]; if (!def) return;
      const hdr = document.createElement("div"); hdr.className = "fhdr";
      hdr.innerHTML = `<input type="checkbox" ${f.active ? "checked" : ""} title="active">
        <span class="fname ${f.active ? "" : "off"}">${def.label}</span>
        <span class="mv up" title="move up">▲</span><span class="mv dn" title="move down">▼</span>`;
      hdr.querySelector("input").onchange = (ev) => { f.active = ev.target.checked; buildControls(); };
      hdr.querySelector(".up").onclick = () => { if (idx > 0) { [list[idx - 1], list[idx]] = [list[idx], list[idx - 1]]; buildControls(); } };
      hdr.querySelector(".dn").onclick = () => { if (idx < list.length - 1) { [list[idx + 1], list[idx]] = [list[idx], list[idx + 1]]; buildControls(); } };
      ctrlPanel.appendChild(hdr);
      if (f.active) for (const p of def.params) ctrlPanel.appendChild(makeControlRow(p, f.params, "r fp"));
    });
  }
  function note(msg) { const n = ctrlPanel.querySelector("#save-note"); if (n) { n.textContent = msg; setTimeout(() => { if (n.textContent === msg) n.textContent = ""; }, 1800); } }

  // Preset file actions — pinned at the top of the panel. All of these operate on the WHOLE
  // preset (every setting getState captures), not just the filter stack.
  function buildActionBar() {
    const bar = document.createElement("div"); bar.className = "sbar top";
    bar.innerHTML = `<button data-a="save" title="Save all changes to this preset">Save</button>
      <button data-a="saveas" title="Save all current settings as a new preset">Save As…</button>
      <button data-a="rename" title="Rename this preset">Rename</button>
      <button data-a="reset" title="Revert to this preset's defaults">Reset</button>
      <span class="note" id="save-note"></span>`;
    bar.querySelector('[data-a="save"]').onclick = savePreset;
    bar.querySelector('[data-a="saveas"]').onclick = saveAsPreset;
    bar.querySelector('[data-a="rename"]').onclick = renamePreset;
    bar.querySelector('[data-a="reset"]').onclick = resetPreset;
    return bar;
  }
  function renamePreset() {
    const e = libEntry(currentId); if (!e) return;
    const name = (prompt("Rename preset:", e.label) || "").trim(); if (!name) return;
    e.label = name; persistLibrary(); refreshOpenBtn();
    const h = ctrlPanel.querySelector("h3"); if (h) h.textContent = name;
    note("renamed");
  }

  function savePreset() { commit(); note("saved"); }
  function saveAsPreset() {
    const name = prompt("Save preset as:", (libEntry(currentId)?.label || "Preset") + " copy");
    if (!name) return;
    let id = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || ("preset-" + library.length);
    while (libEntry(id)) id += "-2";
    const renderer = libEntry(currentId)?.renderer || Object.keys(RENDERERS)[0];
    library.push({ id, label: name, renderer, state: current.getState() });
    persistLibrary(); select(id); note("saved as “" + name + "”");
  }
  function resetPreset() {
    if (defaultStates[currentId] && current.setState) current.setState(defaultStates[currentId]);
    commit(); buildControls(); note("reset to defaults");
  }
  function importPreset() {
    const inp = document.createElement("input"); inp.type = "file"; inp.accept = "application/json,.json";
    inp.onchange = () => {
      const file = inp.files?.[0]; if (!file) return;
      const r = new FileReader();
      r.onload = () => {
        try {
          const st = JSON.parse(r.result);
          const base = file.name.replace(/\.preset\.json$|\.json$/i, "") || "imported";
          let id = base.toLowerCase().replace(/[^a-z0-9]+/g, "-") || "imported"; while (libEntry(id)) id += "-2";
          library.push({ id, label: base, renderer: st.renderer || Object.keys(RENDERERS)[0], state: st });
          persistLibrary(); dialog.classList.remove("open"); select(id); note("imported");
        } catch { note("bad file"); }
      };
      r.readAsText(file);
    };
    inp.click();
  }

  function select(id) {
    if (current) { commit(); app.stage.removeChild(current.container); current.destroy(); }
    const def = libEntry(id) || library[0];
    const factory = RENDERERS[def.renderer] || RENDERERS[Object.keys(RENDERERS)[0]];
    current = factory(app); currentId = def.id;
    app.stage.addChild(current.container);
    // capture the renderer's pristine defaults on first use (before any state is applied),
    // since CFG is module-level and gets mutated by later presets.
    if (!rendererDefaults[def.renderer] && current.getState) rendererDefaults[def.renderer] = current.getState();
    if (!defaultStates[def.id]) {
      const builtin = PRESETS.find((p) => p.id === def.id);
      defaultStates[def.id] = (builtin && builtin.state) ? builtin.state : rendererDefaults[def.renderer];
    }
    if (def.state && current.setState) current.setState(def.state);
    try { localStorage.setItem(LAST_KEY, def.id); } catch {}
    refreshOpenBtn(); buildControls();
  }

  addEventListener("keydown", (e) => {
    if (e.key === "Escape") dialog.classList.remove("open");
    const i = parseInt(e.key, 10) - 1;
    if (i >= 0 && i < library.length) select(library[i].id);
  });
  addEventListener("beforeunload", commit); // never lose edits

  app.ticker.add((t) => { const frame = getFrame(); if (current && frame) current.update(frame, Math.min(0.05, t.deltaMS / 1000)); });

  let startId; try { startId = localStorage.getItem(LAST_KEY); } catch {}
  select(libEntry(startId) ? startId : library[0].id);
  return { app, select };
}
