// lora-companion frontend: extends rgthree's Power Lora Loader without patching rgthree.
//  * LoRA info dialog: Usage / Rating (like-dislike counters) / LoRA Type / Description rows,
//    civitai.red links, Additional Notes edit box keeps the existing text.
//  * "Choose a lora" menu: "(uses)" after each LoRA name (CSS ::after, textContent unchanged),
//    and the row's current LoRA is highlighted and scrolled into view.
// Everything is best-effort: if rgthree internals change, a warning is logged and that part is skipped.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAG = "[lora-companion]";
const PLL_TYPE = "Power Lora Loader (rgthree)";
const STATS = new Map(); // relname -> {uses, likes, dislikes, last_used}

// ---------- stats ----------

async function loadAllStats() {
  try {
    const res = await api.fetchApi("/lora-companion/stats");
    const data = await res.json();
    STATS.clear();
    for (const [k, v] of Object.entries(data.stats || {})) STATS.set(k, v);
  } catch (e) {
    console.warn(TAG, "loading stats failed", e);
  }
}

function applyStats(stats) {
  for (const [file, s] of Object.entries(stats || {})) {
    STATS.set(file, s);
    for (const el of document.querySelectorAll("[data-lc-file]")) {
      if (el.dataset.lcFile === file) renderStats(el, s);
    }
  }
}

async function vote(file, kind, delta) {
  try {
    const res = await api.fetchApi("/lora-companion/vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, kind, delta }),
    });
    const data = await res.json();
    if (data.stats) applyStats({ [file]: data.stats });
    else console.warn(TAG, "vote failed", data);
  } catch (e) {
    console.warn(TAG, "vote failed", e);
  }
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------- info dialog ----------

function el(tag, props = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") e.textContent = v;
    else if (k === "html") e.innerHTML = v;
    else if (k === "style") e.style.cssText = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

// Same markup as rgthree's infoTableRow (non-editable).
function infoRow(label, help, content) {
  const name = el("span", {}, [label + " "]);
  if (help) name.append(el("span", { class: "-help", title: help }));
  const td2 = el("td", { colspan: "2" });
  if (content != null) td2.append(content);
  return el("tr", { "data-lc-row": label }, [el("td", {}, [name]), td2]);
}

function findRow(table, label) {
  for (const tr of table.querySelectorAll(":scope > tbody > tr, :scope > tr")) {
    const first = tr.querySelector("td");
    if (first && first.textContent.trim().startsWith(label)) return tr;
  }
  return null;
}

function insertAfterFirst(table, labels, row) {
  for (const label of labels) {
    const ref = findRow(table, label);
    if (ref) {
      ref.after(row);
      return;
    }
  }
  (table.tBodies[0] || table).append(row);
}

function counter(file, kind, icon) {
  const btn = (delta, text, title) =>
    el("button", { class: "lc-vote-btn", "data-lc-vote": kind, "data-lc-delta": String(delta), title }, [text]);
  return el("span", { class: "lc-counter" }, [
    el("span", { class: "lc-icon" }, [icon]),
    btn(-1, "−", `${kind} −1`),
    el("b", { "data-lc-count": kind }, ["0"]),
    btn(1, "+", `${kind} +1`),
  ]);
}

function renderStats(container, s) {
  s = s || { uses: 0, likes: 0, dislikes: 0, last_used: null };
  const uses = container.querySelector("[data-lc-uses]");
  if (uses) {
    uses.textContent = `${s.uses} run${s.uses === 1 ? "" : "s"}` + (s.last_used ? ` · last ${fmtTime(s.last_used)}` : "");
  }
  for (const [kind, key] of [["like", "likes"], ["dislike", "dislikes"]]) {
    const b = container.querySelector(`[data-lc-count="${kind}"]`);
    if (b) b.textContent = String(s[key] ?? 0);
  }
}

function descriptionMarkup(html) {
  // Plain text only: Civitai HTML is never rendered.
  const PREVIEW = 300;
  const blocks = html.replace(/<br\s*\/?>|<\/(p|div|li|h\d)>/gi, "$&\n");
  const body = new DOMParser().parseFromString(blocks, "text/html").body;
  body.querySelectorAll("script, style").forEach((e) => e.remove());
  const text = (body.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
  const wrap = el("div", { class: "rgthree-info-description", style: "white-space: pre-wrap;" });
  if (text.length <= PREVIEW) {
    wrap.append(el("span", { text }));
    return wrap;
  }
  const preview = el("span", { text: text.slice(0, PREVIEW).trimEnd() + "…" });
  const full = el("span", { text, style: "display: none;" });
  const toggle = el("small", {
    role: "button",
    "data-lc-action": "toggle-description",
    style: "cursor: pointer; text-decoration: underline; opacity: 0.66;",
  }, ["more"]);
  wrap.append(preview, full, " ", toggle);
  return wrap;
}

function decorateInfo(dialog, div) {
  const info = dialog.modelInfo || {};
  const file = info.file;
  const table = div.querySelector("table.rgthree-info-table");
  if (!file || !table) return;

  // civitai.com -> civitai.red
  for (const a of div.querySelectorAll('a[href*="//civitai.com/"]')) {
    a.href = a.getAttribute("href").replace("//civitai.com/", "//civitai.red/");
  }

  // Usage + Rating, before Name.
  const usage = infoRow("Usage", "Successful runs that used this LoRA (lora-companion).",
    el("span", { "data-lc-uses": "" }, ["…"]));
  const rating = infoRow("Rating", "Like / dislike counters. Click + or − (never below 0).",
    el("span", {}, [counter(file, "like", "👍"), counter(file, "dislike", "👎")]));
  const nameRow = findRow(table, "Name");
  if (nameRow) {
    nameRow.before(usage, rating);
  } else {
    (table.tBodies[0] || table).append(usage, rating);
  }
  // Mark both rows so live updates find them.
  for (const r of [usage, rating]) r.setAttribute("data-lc-file", file);
  renderStats(usage, STATS.get(file));
  renderStats(rating, STATS.get(file));

  // LoRA Type, filled asynchronously.
  const typeCell = el("span", {}, ["…"]);
  const typeRow = infoRow("LoRA Type",
    "How the LoRA patches the weights, read from its key names. Plain LoRA/LoHa/LoKr/Diff are added on top of the weight, so their order in the Power Lora Loader does not matter. DoRA, OFT, BOFT and GLoRA read or multiply the current weight and Set overwrites it, so their result depends on which LoRAs are applied before them.",
    typeCell);
  insertAfterFirst(table, ["Base Model", "Name"], typeRow);

  // Civitai version description.
  const desc = info.raw?.civitai?.description;
  if (desc) {
    insertAfterFirst(table, ["Trained Words", "LoRA Type", "Base Model", "Name"],
      infoRow("Description", "The civitai description of this version, as plain text.", descriptionMarkup(desc)));
  }

  div.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-lc-vote],[data-lc-action]");
    if (!t || !div.contains(t)) return;
    e.preventDefault();
    e.stopPropagation();
    if (t.dataset.lcVote) {
      await vote(file, t.dataset.lcVote, Number(t.dataset.lcDelta));
    } else if (t.dataset.lcAction === "toggle-description") {
      const [preview, full] = t.closest(".rgthree-info-description").children;
      const expanding = full.style.display === "none";
      preview.style.display = expanding ? "none" : "";
      full.style.display = expanding ? "" : "none";
      t.textContent = expanding ? "less" : "more";
    }
  });

  api.fetchApi(`/lora-companion/info?file=${encodeURIComponent(file)}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data) {
        typeRow.remove();
        return;
      }
      if (data.stats) applyStats({ [file]: data.stats });
      const lt = data.loraType;
      if (!lt) {
        typeRow.remove();
        return;
      }
      typeCell.textContent = lt.label;
      if (lt.orderSensitive) typeCell.append(" ", el("i", {}, ["(order sensitive)"]));
    })
    .catch((e) => {
      typeRow.remove();
      console.warn(TAG, e);
    });
}

async function patchInfoDialog() {
  let mod;
  try {
    mod = await import("../rgthree-comfy/dialog_info.js");
  } catch (e) {
    console.warn(TAG, "rgthree dialog_info.js not available; info dialog extras disabled", e);
    return;
  }
  const LoraDialog = mod.RgthreeLoraInfoDialog;
  const proto = LoraDialog?.prototype;
  const base = proto && Object.getPrototypeOf(proto);
  if (typeof proto?.getInfoContent !== "function" || typeof base?.handleEventAction !== "function") {
    console.warn(TAG, "rgthree info dialog changed; info dialog extras disabled");
    return;
  }
  if (proto.__lcPatched) return;
  proto.__lcPatched = true;

  // Own method on the LoRA dialog only (the checkpoint dialog shares the base class).
  const origContent = proto.getInfoContent;
  proto.getInfoContent = function () {
    const div = origContent.apply(this, arguments);
    try {
      if (div instanceof Element) decorateInfo(this, div);
    } catch (e) {
      console.warn(TAG, "decorating info dialog failed", e);
    }
    return div;
  };

  // Additional Notes edit box opened empty (rgthree sets textarea "value" as an attribute).
  // Fixes both LoRA and checkpoint dialogs.
  const origAction = base.handleEventAction;
  base.handleEventAction = async function (action, target, e) {
    let tr = null;
    let text = null;
    if (action === "edit-row") {
      tr = target?.closest?.("tr");
      const td = tr?.querySelector("td:nth-child(2)");
      if (td && !td.querySelector("input,textarea")) text = td.textContent;
    }
    const r = await origAction.apply(this, arguments);
    if (text != null) {
      const ta = tr.querySelector("td:nth-child(2) textarea");
      if (ta && !ta.value) ta.value = text;
    }
    return r;
  };
}

// ---------- LoRA chooser: counts + highlight current ----------

let pendingCurrent = null; // {value, t}: LoRA of the row whose name was clicked
let pendingPrefix = null; // {prefix, t}: prefix the Power Lora Loader stripped from the entries
const MATCH_PROP = "Match"; // rgthree PROP_LABEL_LORA_MATCH
let rgthreeApi = null;

// Same computation as rgthree's RgthreePowerLoraLoader.showLoraChooser: with a "Match" regex set,
// the list is filtered and the common prefix of the remaining files is removed from every entry.
async function chooserPrefix(node) {
  const match = node?.properties?.[MATCH_PROP];
  if (!match || !rgthreeApi) return "";
  let loras = (await rgthreeApi.getLoras()).map((l) => l.file);
  try {
    const rgx = new RegExp(match);
    loras = loras.filter((l) => l.match(rgx));
  } catch {
    return "";
  }
  let prefix = loras[0] || "";
  for (const l of loras) {
    let i = 0;
    while (prefix[i] && prefix[i] === l[i]) i++;
    prefix = prefix.slice(0, i);
    if (!prefix) break;
  }
  return prefix;
}

function selectMenuEntry(menu, value) {
  // Moves the menu filter's own selection (ArrowDown) so keyboard state and highlight agree.
  const entries = [...menu.querySelectorAll(".litemenu-entry")];
  const index = entries.findIndex((e) => e.textContent === value);
  if (index < 0) return;
  const filter = menu.querySelector(".comfy-context-menu-filter");
  filter?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
  for (let i = 0; i < index; i++) filter?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
  filter?.focus({ preventScroll: true });
  const entry = entries[index];
  menu.scrollTop = entry.offsetTop - (menu.clientHeight - entry.offsetHeight) / 2;
}

function onMenu(menu) {
  const title = menu.querySelector(".litemenu-title");
  if (!title || title.textContent !== "Choose a lora") return;
  const pp = pendingPrefix;
  const pc = pendingCurrent;
  pendingPrefix = null;
  pendingCurrent = null;
  const prefix = pp && Date.now() - pp.t < 5000 ? pp.prefix : "";
  for (const entry of menu.querySelectorAll(".litemenu-entry")) {
    const uses = STATS.get(prefix + entry.textContent)?.uses;
    if (uses > 0) entry.dataset.lcCount = String(uses);
  }
  if (pc && Date.now() - pc.t < 5000 && pc.value?.startsWith(prefix)) {
    const value = pc.value.slice(prefix.length);
    requestAnimationFrame(() => selectMenuEntry(menu, value));
  }
}

function watchMenus() {
  new MutationObserver((records) => {
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType === 1 && n.classList.contains("litecontextmenu")) onMenu(n);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}

function patchLoraWidget(w) {
  const area = w?.hitAreas?.lora;
  if (!area || area.__lcPatched || typeof area.onClick !== "function") return;
  const orig = area.onClick;
  area.onClick = function () {
    pendingCurrent = { value: this.value?.lora, t: Date.now() };
    return orig.apply(this, arguments);
  };
  area.__lcPatched = true;
}

function patchPllClass() {
  const cls = LiteGraph.registered_node_types?.[PLL_TYPE];
  const proto = cls?.prototype;
  if (typeof proto?.addNewLoraWidget !== "function") {
    console.warn(TAG, "Power Lora Loader not found; chooser highlight disabled");
    return;
  }
  if (proto.__lcPatched) return;
  proto.__lcPatched = true;
  if (typeof proto.showLoraChooser === "function") {
    const origChooser = proto.showLoraChooser;
    proto.showLoraChooser = async function () {
      // Set before rgthree builds the menu (it awaits the same cached getLoras()).
      let prefix = "";
      try {
        prefix = await chooserPrefix(this);
      } catch (e) {
        console.warn(TAG, e);
      }
      pendingPrefix = { prefix, t: Date.now() };
      return origChooser.apply(this, arguments);
    };
  }
  const orig = proto.addNewLoraWidget;
  proto.addNewLoraWidget = function () {
    const w = orig.apply(this, arguments);
    try {
      patchLoraWidget(w);
    } catch (e) {
      console.warn(TAG, e);
    }
    return w;
  };
}

function patchExistingNodes() {
  for (const node of app.graph?._nodes || []) {
    if (node.type === PLL_TYPE || node.comfyClass === PLL_TYPE) {
      for (const w of node.widgets || []) patchLoraWidget(w);
    }
  }
}

// ---------- extension ----------

const CSS = `
.litecontextmenu .litemenu-entry[data-lc-count]::after {
  content: " (" attr(data-lc-count) ")";
  opacity: 0.55;
}
.lc-counter { display: inline-flex; align-items: center; gap: 4px; margin-right: 16px; }
.lc-counter b { min-width: 1.5em; text-align: center; }
.lc-vote-btn {
  cursor: pointer; border: 1px solid rgba(255,255,255,0.25); background: rgba(255,255,255,0.06);
  color: inherit; border-radius: 4px; width: 22px; height: 20px; line-height: 16px; padding: 0;
}
.lc-vote-btn:hover { background: rgba(255,255,255,0.18); }
`;

app.registerExtension({
  name: "lora-companion",
  async setup() {
    document.head.append(el("style", { text: CSS }));
    api.addEventListener("lora-companion.stats", (e) => applyStats(e.detail?.stats));
    // After a reconnect (server restart) the cached counts may be stale.
    api.addEventListener("reconnected", () => loadAllStats());
    watchMenus();
    try {
      ({ rgthreeApi } = await import("../../rgthree/common/rgthree_api.js"));
    } catch (e) {
      console.warn(TAG, "rgthree api not available; Match prefix not handled", e);
    }
    try {
      patchPllClass();
      patchExistingNodes();
    } catch (e) {
      console.warn(TAG, e);
    }
    await Promise.all([loadAllStats(), patchInfoDialog()]);
  },
  nodeCreated(node) {
    if (node.type === PLL_TYPE || node.comfyClass === PLL_TYPE) {
      for (const w of node.widgets || []) patchLoraWidget(w);
    }
  },
});
