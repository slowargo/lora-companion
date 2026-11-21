// lora-companion frontend. Extends rgthree's Power Lora Loader without patching rgthree:
//  * LoRA info dialog: Usage and Rating (like/dislike) rows, LoRA Type, Civitai description,
//    civitai.red links, and the Additional Notes edit box keeps its text.
//  * "Choose a lora" menu: "(uses)" after each LoRA and the row's current LoRA highlighted.
//  * Loader rows load their LoRA info (info icon, strength min/max hints) reliably.
// rgthree internals are reached through dynamic imports and checked before use; if something is
// missing, that feature is skipped with a console warning.
//
// Things that do not work with the current frontend and must be avoided:
//  * Patching a row widget's prototype. LGraphNode.addCustomWidget copies the widget's prototype
//    members onto the instance and swaps its prototype for a shared LegacyWidget class.
//  * Relying on the row's `value` setter. The frontend merges `value` with its own accessor and
//    runs rgthree's setter before storing the new value, so the getLoraInfo() that setter calls
//    still reads the old (empty) LoRA on workflow load and Duplicate.
//  * Relying on rgthree's info cache being refreshed by its ws event: LoraInfoService registers
//    its listener before its event name is assigned, so it never receives it.
// Info dialogs and node classes are not adopted that way; patching their prototypes is fine.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAG = "[lora-companion]";
const PLL_TYPE = "Power Lora Loader (rgthree)";
const MATCH_PROP = "Match"; // rgthree PROP_LABEL_LORA_MATCH

const rgthree = Promise.all([
  import("../rgthree-comfy/dialog_info.js"),
  import("../../rgthree/common/model_info_service.js"),
  import("../../rgthree/common/rgthree_api.js"),
]).then(([dialogs, infoService, rgApi]) => ({
  LoraDialog: dialogs.RgthreeLoraInfoDialog,
  infoService: infoService.LORA_INFO_SERVICE,
  rgthreeApi: rgApi.rgthreeApi,
}));
rgthree.catch((e) => console.warn(TAG, "rgthree modules not available; Power Lora Loader extras disabled", e));

// ---------- usage stats ----------

const STATS = new Map(); // lora file -> {uses, likes, dislikes, last_used}

async function loadAllStats() {
  try {
    const data = await (await api.fetchApi("/lora-companion/stats")).json();
    STATS.clear();
    for (const [file, s] of Object.entries(data.stats || {})) STATS.set(file, s);
  } catch (e) {
    console.warn(TAG, "loading stats failed", e);
  }
}

function applyStats(stats) {
  for (const [file, s] of Object.entries(stats || {})) {
    STATS.set(file, s);
    for (const box of document.querySelectorAll("[data-lc-file]")) {
      if (box.dataset.lcFile === file) renderStats(box, s);
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

// ---------- loader rows: LoRA info ----------

// Bumped when the server reports refreshed info for a LoRA (e.g. a Civitai fetch in the dialog),
// so rows showing that LoRA load it again.
const INFO_VERSION = new Map();

function infoKey(lora) {
  return `${lora}\n${INFO_VERSION.get(lora) ?? 0}`;
}

const RETRY_MS = 5000;

// Replaces rgthree's getLoraInfo(force) on one row: same service call, but cached per
// LoRA + version instead of once per row. The row's latest result (lcInfo) is authoritative:
// draw() loads it whenever the key changed (workflow load, Duplicate, chooser, info refresh) and
// puts it back if rgthree code cleared or overwrote loraInfo (onLoraClick sets it to null; a
// response from rgthree's original getLoraInfo can land late). A failed request is retried.
function patchRowInfo(w, infoService) {
  w.getLoraInfo = function (force = false) {
    const lora = this.value?.lora;
    const key = infoKey(lora);
    if (!force && this.loraInfoPromise && this.lcInfoKey === key) return this.loraInfoPromise;
    this.lcInfoKey = key;
    this.lcInfoReady = false;
    const fetched = lora && lora !== "None" ? infoService.getInfo(lora, force, true) : Promise.resolve(null);
    const promise = fetched.then(
      (info) => {
        if (this.loraInfoPromise === promise) {
          this.lcInfo = this.loraInfo = info;
          this.lcInfoReady = true;
          app.canvas?.setDirty(true, true);
        }
        return info;
      },
      (e) => {
        if (this.loraInfoPromise === promise) {
          this.lcInfoKey = null;
          this.lcRetryAt = Date.now() + RETRY_MS;
        }
        console.warn(TAG, "loading LoRA info failed", lora, e);
        return null;
      },
    );
    this.loraInfoPromise = promise;
    return promise;
  };
  const draw = w.draw;
  w.draw = function () {
    if (this.lcInfoKey !== infoKey(this.value?.lora)) {
      if (!(this.lcRetryAt > Date.now())) this.getLoraInfo();
    } else if (this.lcInfoReady && this.loraInfo !== this.lcInfo) {
      this.loraInfo = this.lcInfo;
    }
    return draw.apply(this, arguments);
  };
  w.loraInfoPromise = null; // drop whatever the original fetched for the initial empty value
}

// ---------- loader rows: chooser ----------

let pendingCurrent = null; // {value, t}: LoRA of the row whose name was just clicked
let pendingPrefix = null; // {prefix, t}: prefix the node's Match property strips from entries

function patchRowChooser(w) {
  const area = w.hitAreas.lora;
  const onClick = area.onClick;
  area.onClick = function () {
    pendingCurrent = { value: this.value?.lora, t: Date.now() };
    return onClick.apply(this, arguments);
  };
}

function isLoraRow(w) {
  return typeof w?.getLoraInfo === "function" && typeof w.draw === "function" && typeof w.hitAreas?.lora?.onClick === "function";
}

// Every row is added through addCustomWidget, on any node in any (sub)graph, before its value is
// assigned. Installed at module load, ahead of the first workflow load.
{
  const proto = (globalThis.LGraphNode ?? LiteGraph.LGraphNode)?.prototype;
  const addCustomWidget = proto?.addCustomWidget;
  if (typeof addCustomWidget !== "function") {
    console.warn(TAG, "LGraphNode.addCustomWidget not found; row info fix and chooser highlight disabled");
  } else {
    let infoService = null;
    rgthree.then((m) => (infoService = m.infoService), () => {});
    proto.addCustomWidget = function () {
      const w = addCustomWidget.apply(this, arguments);
      if (isLoraRow(w) && !w.lcPatched) {
        w.lcPatched = true;
        patchRowChooser(w);
        if (infoService) patchRowInfo(w, infoService);
        else rgthree.then((m) => patchRowInfo(w, m.infoService), () => {});
      }
      return w;
    };
  }
}

// Same computation as rgthree's RgthreePowerLoraLoader.showLoraChooser.
async function chooserPrefix(node, rgthreeApi) {
  const match = node?.properties?.[MATCH_PROP];
  if (!match) return "";
  let rgx;
  try {
    rgx = new RegExp(match);
  } catch {
    return "";
  }
  const loras = (await rgthreeApi.getLoras()).map((l) => l.file).filter((l) => l.match(rgx));
  let prefix = loras[0] || "";
  for (const l of loras) {
    let i = 0;
    while (prefix[i] && prefix[i] === l[i]) i++;
    prefix = prefix.slice(0, i);
    if (!prefix) break;
  }
  return prefix;
}

function patchChooserPrefix(rgthreeApi) {
  const proto = LiteGraph.registered_node_types?.[PLL_TYPE]?.prototype;
  if (typeof proto?.showLoraChooser !== "function") {
    console.warn(TAG, "Power Lora Loader not found; Match prefix not handled");
    return;
  }
  const showLoraChooser = proto.showLoraChooser;
  proto.showLoraChooser = async function () {
    let prefix = "";
    try {
      prefix = await chooserPrefix(this, rgthreeApi);
    } catch (e) {
      console.warn(TAG, e);
    }
    pendingPrefix = { prefix, t: Date.now() };
    return showLoraChooser.apply(this, arguments);
  };
}

// Moves the menu filter's own selection (it handles ArrowDown) so keyboard state and highlight agree.
function selectMenuEntry(menu, value) {
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

function onChooserMenu(menu) {
  const fresh = (p) => (p && Date.now() - p.t < 5000 ? p : null);
  const prefix = fresh(pendingPrefix)?.prefix ?? "";
  const current = fresh(pendingCurrent)?.value;
  pendingPrefix = pendingCurrent = null;
  for (const entry of menu.querySelectorAll(".litemenu-entry")) {
    const uses = STATS.get(prefix + entry.textContent)?.uses;
    if (uses > 0) entry.dataset.lcCount = String(uses);
  }
  // The filter is set up after the menu is inserted.
  if (current?.startsWith(prefix)) requestAnimationFrame(() => selectMenuEntry(menu, current.slice(prefix.length)));
}

function watchChooserMenus() {
  new MutationObserver((records) => {
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType === 1 && n.classList.contains("litecontextmenu") &&
            n.querySelector(".litemenu-title")?.textContent === "Choose a lora") {
          onChooserMenu(n);
        }
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}

// ---------- info dialog ----------

function el(tag, props = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") e.textContent = v;
    else if (k === "style") e.style.cssText = v;
    else e.setAttribute(k, v);
  }
  e.append(...children);
  return e;
}

// Same markup as rgthree's (non-editable) infoTableRow.
function infoRow(label, help, content) {
  const name = el("span", {}, [label + " ", el("span", { class: "-help", title: help })]);
  return el("tr", { "data-lc-row": label }, [el("td", {}, [name]), el("td", { colspan: "2" }, [content])]);
}

function findRow(table, label) {
  return [...table.rows].find((tr) => tr.cells[0]?.textContent.trim().startsWith(label)) ?? null;
}

function insertAfterFirst(table, labels, row) {
  const ref = labels.map((l) => findRow(table, l)).find(Boolean);
  if (ref) ref.after(row);
  else (table.tBodies[0] ?? table).append(row);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderStats(box, s = { uses: 0, likes: 0, dislikes: 0, last_used: null }) {
  const uses = box.querySelector("[data-lc-uses]");
  if (uses) uses.textContent = `${s.uses} run${s.uses === 1 ? "" : "s"}` + (s.last_used ? ` · last ${fmtTime(s.last_used)}` : "");
  for (const [kind, key] of [["like", "likes"], ["dislike", "dislikes"]]) {
    const b = box.querySelector(`[data-lc-count="${kind}"]`);
    if (b) b.textContent = String(s[key] ?? 0);
  }
}

function counter(kind, icon) {
  const btn = (delta, text) => el("button", { class: "lc-vote-btn", "data-lc-vote": kind, "data-lc-delta": String(delta), title: `${kind} ${text}1` }, [text]);
  return el("span", { class: "lc-counter" }, [
    el("span", {}, [icon]),
    el("b", { "data-lc-count": kind }, ["0"]),
    el("span", { class: "lc-vote-stack" }, [btn(1, "+"), btn(-1, "−")]),
  ]);
}

// Civitai's description HTML, shown as plain text (never rendered).
function descriptionMarkup(html) {
  const PREVIEW = 300;
  const body = new DOMParser().parseFromString(html.replace(/<br\s*\/?>|<\/(p|div|li|h\d)>/gi, "$&\n"), "text/html").body;
  body.querySelectorAll("script, style").forEach((e) => e.remove());
  const text = (body.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
  const wrap = el("div", { class: "rgthree-info-description", style: "white-space: pre-wrap;" });
  if (text.length <= PREVIEW) {
    wrap.append(el("span", { text }));
  } else {
    wrap.append(
      el("span", { text: text.slice(0, PREVIEW).trimEnd() + "…" }),
      el("span", { text, style: "display: none;" }),
      " ",
      el("small", { role: "button", "data-lc-action": "toggle-description", style: "cursor: pointer; text-decoration: underline; opacity: 0.66;" }, ["more"]),
    );
  }
  return wrap;
}

const LORA_TYPE_HELP = "How the LoRA patches the weights, read from its key names. Plain LoRA/LoHa/LoKr/Diff are added on top of the weight, so their order in the Power Lora Loader does not matter. DoRA, OFT, BOFT and GLoRA read or multiply the current weight and Set overwrites it, so their result depends on which LoRAs are applied before them.";

function decorateInfo(info, div) {
  const file = info?.file;
  const table = div.querySelector("table.rgthree-info-table");
  if (!file || !table) return;

  for (const a of div.querySelectorAll('a[href*="//civitai.com/"]')) {
    a.href = a.getAttribute("href").replace("//civitai.com/", "//civitai.red/");
  }

  const usage = infoRow("Usage", "Successful runs that used this LoRA.", el("span", { "data-lc-uses": "" }, ["…"]));
  const rating = infoRow("Rating", "Like / dislike counters (never below 0).", el("span", {}, [counter("like", "👍"), counter("dislike", "👎")]));
  for (const r of [usage, rating]) {
    r.dataset.lcFile = file;
    renderStats(r, STATS.get(file));
  }
  const nameRow = findRow(table, "Name");
  if (nameRow) nameRow.before(usage, rating);
  else (table.tBodies[0] ?? table).append(usage, rating);

  const typeCell = el("span", {}, ["…"]);
  const typeRow = infoRow("LoRA Type", LORA_TYPE_HELP, typeCell);
  insertAfterFirst(table, ["Base Model", "Name"], typeRow);

  const desc = info.raw?.civitai?.description;
  if (desc) {
    insertAfterFirst(table, ["Trained Words", "LoRA Type", "Base Model", "Name"],
      infoRow("Description", "The Civitai description of this version, as plain text.", descriptionMarkup(desc)));
  }

  div.addEventListener("click", (e) => {
    const t = e.target.closest("[data-lc-vote],[data-lc-action]");
    if (!t) return;
    e.preventDefault();
    e.stopPropagation();
    if (t.dataset.lcVote) {
      vote(file, t.dataset.lcVote, Number(t.dataset.lcDelta));
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
      if (data?.stats) applyStats({ [file]: data.stats });
      const lt = data?.loraType;
      if (!lt) return typeRow.remove();
      typeCell.textContent = lt.label;
      if (lt.orderSensitive) typeCell.append(" ", el("i", {}, ["(order sensitive)"]));
    })
    .catch((e) => {
      typeRow.remove();
      console.warn(TAG, e);
    });
}

function patchInfoDialog(LoraDialog) {
  const proto = LoraDialog?.prototype;
  const base = proto && Object.getPrototypeOf(proto);
  if (typeof proto?.getInfoContent !== "function" || typeof base?.handleEventAction !== "function") {
    console.warn(TAG, "rgthree info dialog changed; info dialog extras disabled");
    return;
  }
  // Defined on the LoRA dialog only; the checkpoint dialog shares the base class.
  const getInfoContent = proto.getInfoContent;
  proto.getInfoContent = function () {
    const div = getInfoContent.apply(this, arguments);
    try {
      if (div instanceof Element) decorateInfo(this.modelInfo, div);
    } catch (e) {
      console.warn(TAG, "decorating info dialog failed", e);
    }
    return div;
  };
  // rgthree creates the Additional Notes textarea with a "value" attribute, which a textarea
  // ignores, so editing starts empty and saving would wipe the note.
  const handleEventAction = base.handleEventAction;
  base.handleEventAction = async function (action, target) {
    const tr = action === "edit-row" ? target?.closest?.("tr") : null;
    const td = tr?.cells[1];
    const text = td && !td.querySelector("input,textarea") ? td.textContent : null;
    const result = await handleEventAction.apply(this, arguments);
    const ta = text != null ? td.querySelector("textarea") : null;
    if (ta && !ta.value) ta.value = text;
    return result;
  };
}

// ---------- extension ----------

const CSS = `
.litecontextmenu .litemenu-entry[data-lc-count]::after { content: " (" attr(data-lc-count) ")"; opacity: 0.55; }
.lc-counter { display: inline-flex; align-items: center; gap: 4px; margin-right: 16px; }
.lc-counter b { min-width: 1.5em; text-align: center; }
.lc-vote-stack { display: inline-flex; flex-direction: column; gap: 1px; }
.lc-vote-btn {
  cursor: pointer; border: 1px solid rgba(255,255,255,0.25); background: rgba(255,255,255,0.06);
  color: inherit; border-radius: 3px; width: 18px; height: 12px; line-height: 10px; font-size: 11px; padding: 0;
}
.lc-vote-btn:hover { background: rgba(255,255,255,0.18); }
`;

app.registerExtension({
  name: "lora-companion",
  async setup() {
    document.head.append(el("style", { text: CSS }));
    api.addEventListener("lora-companion.stats", (e) => applyStats(e.detail?.stats));
    api.addEventListener("reconnected", loadAllStats);
    api.addEventListener("rgthree-refreshed-loras-info", async (e) => {
      const info = e.detail?.data;
      if (!info?.file) return;
      // rgthree's own listener never fires (see top); refresh its cache before rows refetch,
      // or they may read the old entry while the refresh request is still in flight.
      try {
        (await rgthree).infoService.setFreshInfo(info.file, info);
      } catch {
        // already reported
      }
      INFO_VERSION.set(info.file, (INFO_VERSION.get(info.file) ?? 0) + 1);
      app.canvas?.setDirty(true, true);
    });
    watchChooserMenus();
    const statsLoaded = loadAllStats();
    try {
      const { LoraDialog, rgthreeApi } = await rgthree;
      patchInfoDialog(LoraDialog);
      patchChooserPrefix(rgthreeApi);
    } catch {
      // already reported
    }
    await statsLoaded;
  },
});
