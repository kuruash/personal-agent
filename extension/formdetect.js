// Generic form discovery + fill.
//
// This file has ZERO site-specific selectors. The detector works against
// browser semantics — native form primitives (input / textarea / select)
// AND ARIA custom controls (role="combobox|textbox|radio|checkbox|
// listbox") — plus recursive traversal of OPEN shadow roots. Iframe
// coverage is handled outside this file by background.js, which runs
// this module in every frame via chrome.scripting.executeScript(
// {allFrames: true}).
//
// Public API (unchanged shape, extended fields):
//
//   detectFormFields(scope = document)
//     → [ Field ]
//
//     Field = {
//       selector:    stable CSS selector, relative to `shadowPath` root
//       shadowPath:  array of host selectors traversing shadow DOM;
//                    empty [] for controls in the light DOM
//       type:        canonical control type — text / textarea / select /
//                    combobox / radio_group / checkbox_group /
//                    date / number / file / url / email / tel / password /
//                    ...
//       tag:         underlying tag name (input/textarea/select/div/…)
//       label:       best-guess question text
//       label_source:strategy that produced the label
//       options:     [{value, text}] for select / combobox /
//                    radio_group / checkbox_group; else undefined
//       required, current_value, name, id, autocomplete, placeholder,
//       aria_label, group_size
//     }
//
//   fillField(selector, value, shadowPath = [])
//     → { ok, filled?, error? }
//
// Debug mode: set PA_FORM_DEBUG below to true, or run
//   chrome.storage.local.set({ PA_FORM_DEBUG: true })
// once from the extension console — the detector will then console.log
// contexts / raw counts / rejection reasons / final field summaries.
// User-entered field values are NEVER logged.

(function (root) {
  // ──────────── config ────────────

  // Compile-time default. Runtime override lives in chrome.storage.local
  // under key "PA_FORM_DEBUG" (checked once at module load below).
  let PA_FORM_DEBUG = false;

  // Native input types we NEVER treat as fillable form fields.
  const SKIP_INPUT_TYPES = new Set([
    "submit", "button", "reset", "image", "hidden", "file", "password",
  ]);

  // Input types where the platform value is the meaningful surface.
  const NATIVE_INPUT_TYPES = new Set([
    "text", "email", "tel", "url", "number", "search", "date",
    "datetime-local", "month", "week", "time", "color", "range",
  ]);

  // Selectors used in raw discovery. Split so ARIA discovery can be
  // filtered separately from native controls when we log rejections.
  const NATIVE_SELECTOR = "input, textarea, select";
  const ARIA_SELECTOR = [
    '[role="textbox"]',
    '[role="combobox"]',
    '[role="listbox"]',
    '[role="radio"]',
    '[role="checkbox"]',
    '[role="switch"]',
  ].join(",");

  // Try to load the runtime debug flag from chrome.storage. Safe no-op
  // when running outside the extension context (e.g. Node harness).
  try {
    if (typeof chrome !== "undefined" && chrome.storage?.local?.get) {
      chrome.storage.local.get(["PA_FORM_DEBUG"], (r) => {
        if (r && r.PA_FORM_DEBUG === true) PA_FORM_DEBUG = true;
      });
    }
  } catch (_) { /* ignore */ }

  const dbg = (...args) => { if (PA_FORM_DEBUG) console.log("[PA form]", ...args); };

  // ──────────── public: detectFormFields ────────────

  function detectFormFields(scope) {
    scope = scope || (typeof document !== "undefined" ? document : null);
    if (!scope) return [];

    const contexts = discoverContexts(scope);
    dbg("contexts scanned:", contexts.length,
        "(", contexts.filter((c) => c.shadowPath.length === 0).length, "light,",
        contexts.filter((c) => c.shadowPath.length > 0).length, "shadow)");

    const rawCandidates = [];
    for (const ctx of contexts) {
      const native = Array.from(ctx.root.querySelectorAll(NATIVE_SELECTOR));
      let aria = [];
      try { aria = Array.from(ctx.root.querySelectorAll(ARIA_SELECTOR)); }
      catch (_) { /* selectors above are safe, but ignore weird docs */ }
      // Union with dedupe by element identity — a native input can also
      // carry role="textbox"; only count it once.
      const seen = new Set();
      for (const el of native.concat(aria)) {
        if (seen.has(el)) continue;
        seen.add(el);
        rawCandidates.push({ el, ctx });
      }
    }
    dbg("raw candidates:", rawCandidates.length);

    const accepted = [];
    const rejected = [];
    for (const c of rawCandidates) {
      const v = classifyVisible(c.el);
      if (!v.ok) { rejected.push({ el: c.el, reason: v.reason }); continue; }
      const type = classifyControl(c.el);
      if (type === null) { rejected.push({ el: c.el, reason: "unclassifiable" }); continue; }
      accepted.push({ el: c.el, ctx: c.ctx, type });
    }
    if (PA_FORM_DEBUG && rejected.length) {
      const grouped = {};
      for (const r of rejected) grouped[r.reason] = (grouped[r.reason] || 0) + 1;
      dbg("accepted:", accepted.length, "rejected:", rejected.length, grouped);
    }

    // Group radio + checkbox controls into single questions.
    const { groupOf, firstMemberOf, groups } = groupChoiceControls(accepted);

    const fields = [];
    const emittedGroups = new Set();
    for (const item of accepted) {
      const grp = groupOf.get(item.el);
      if (grp) {
        if (emittedGroups.has(grp)) continue;
        if (firstMemberOf.get(grp) !== item.el) continue;
        emittedGroups.add(grp);
        fields.push(buildGroupField(grp, item.ctx));
      } else {
        fields.push(buildSingleField(item.el, item.ctx, item.type));
      }
    }
    dbg("groups formed:", groups.length, "final fields:", fields.length);

    const deduped = dedupeFields(fields);
    if (PA_FORM_DEBUG) {
      dbg("final fields (after dedupe):", deduped.length);
      for (const f of deduped) {
        dbg("  ·", JSON.stringify({
          label: f.label, type: f.type,
          options: (f.options && f.options.length) || 0,
          shadowPath: f.shadowPath, selector: f.selector,
        }));
      }
    }
    return deduped;
  }

  // ──────────── context discovery ────────────

  function discoverContexts(scope) {
    // Yields { root, shadowPath } for the light DOM + every reachable
    // open shadow root. Closed shadow roots are unreachable by design.
    const out = [];
    const startRoot = scope.ownerDocument ? scope : scope;
    out.push({ root: startRoot, shadowPath: [] });
    walkShadow(startRoot, [], out);
    return out;
  }

  function walkShadow(root, pathSoFar, out) {
    let all;
    try { all = root.querySelectorAll("*"); }
    catch (_) { return; }
    for (const el of all) {
      if (el.shadowRoot) {
        const step = buildSelector(el);
        const newPath = pathSoFar.concat([step]);
        out.push({ root: el.shadowRoot, shadowPath: newPath });
        walkShadow(el.shadowRoot, newPath, out);
      }
    }
  }

  // ──────────── visibility + acceptance ────────────

  function classifyVisible(el) {
    // Native input type gates first (cheap, definitive).
    if (el.tagName === "INPUT") {
      const t = (el.type || "").toLowerCase();
      if (SKIP_INPUT_TYPES.has(t)) return { ok: false, reason: `input-type-${t}` };
    }
    if (el.disabled) return { ok: false, reason: "disabled" };
    if (el.hasAttribute("hidden")) return { ok: false, reason: "hidden-attr" };
    // aria-hidden is inheritable in the ARIA sense — an ancestor
    // aria-hidden="true" hides the entire subtree from assistive tech,
    // and we treat it the same for form discovery.
    for (let n = el; n; n = n.parentElement) {
      if (n.getAttribute && n.getAttribute("aria-hidden") === "true") {
        return { ok: false, reason: "aria-hidden" };
      }
    }

    // Computed style. Deliberately does NOT use offsetParent or
    // getBoundingClientRect().width — many modern layouts (position:fixed,
    // css transforms, virtualized lists) give false negatives on those.
    try {
      const win = el.ownerDocument && el.ownerDocument.defaultView;
      if (win && win.getComputedStyle) {
        const s = win.getComputedStyle(el);
        if (s.display === "none") return { ok: false, reason: "display-none" };
        if (s.visibility === "hidden" || s.visibility === "collapse") {
          return { ok: false, reason: "visibility-hidden" };
        }
      }
    } catch (_) { /* ignore — treat as visible */ }

    return { ok: true };
  }

  function classifyControl(el) {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();

    if (tag === "textarea") return "textarea";
    if (tag === "select") return el.multiple ? "select-multiple" : "select";
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      if (t === "radio") return "radio";
      if (t === "checkbox") return "checkbox";
      if (NATIVE_INPUT_TYPES.has(t)) return t;
      return "text";
    }
    // ARIA custom controls.
    if (role === "textbox") return "text";
    if (role === "combobox" || role === "listbox") return "combobox";
    if (role === "radio") return "radio";
    if (role === "checkbox" || role === "switch") return "checkbox";
    return null;
  }

  // ──────────── choice grouping ────────────

  function groupChoiceControls(accepted) {
    // Group priority:
    //   1. shared `name` attribute (native radio/checkbox standard)
    //   2. shared role="radiogroup"/<fieldset> ancestor
    //   3. shared nearest common ancestor that contains ONLY radios
    //      or ONLY checkboxes of one logical group (generic fallback,
    //      no site-specific classes)
    const groups = [];
    const byName = new Map();       // "type::name" → group
    const byAncestor = new Map();   // ancestor Element → group
    const groupOf = new Map();
    const firstMemberOf = new Map();

    const choiceItems = accepted.filter(
      (a) => a.type === "radio" || a.type === "checkbox"
    );

    // Pass 1 — shared name.
    for (const item of choiceItems) {
      const name = item.el.getAttribute("name");
      if (!name) continue;
      const key = `${item.type}::${name}`;
      let g = byName.get(key);
      if (!g) {
        g = { kind: item.type, name, ancestor: null, members: [], ctx: item.ctx };
        byName.set(key, g);
        groups.push(g);
      }
      g.members.push(item.el);
      groupOf.set(item.el, g);
    }

    // Pass 2 — role="radiogroup" or <fieldset> ancestor.
    for (const item of choiceItems) {
      if (groupOf.has(item.el)) continue;
      const anc = closestInSameContext(item.el, item.ctx, '[role="radiogroup"], fieldset');
      if (!anc) continue;
      let g = byAncestor.get(anc);
      if (!g) {
        g = { kind: item.type, name: null, ancestor: anc, members: [], ctx: item.ctx };
        byAncestor.set(anc, g);
        groups.push(g);
      }
      g.members.push(item.el);
      groupOf.set(item.el, g);
    }

    // Pass 3 — generic ancestor rescue. Walk up from each ungrouped
    // choice control; the first ancestor whose descendants contain
    // multiple choice controls of the SAME kind and NO other kinds
    // becomes the group container.
    const remaining = choiceItems.filter((it) => !groupOf.has(it.el));
    const groupedByAncestor = new Map(); // ancestor → group
    for (const item of remaining) {
      let node = item.el.parentElement;
      let container = null;
      while (node) {
        const siblings = collectChoiceDescendants(node);
        if (siblings.total >= 2 &&
            siblings.otherKind === 0 &&
            siblings.matchingKind === siblings.total) {
          container = node; break;
        }
        // Bail out early if this ancestor already contains other
        // *non-choice* form controls — the "question container" is
        // then finer-grained than this ancestor, so leave the item
        // ungrouped and let it emit as a singleton.
        if (siblings.total === 1 && ancestorContainsOtherControls(node, item.el)) break;
        node = node.parentElement;
      }
      if (!container) continue;
      let g = groupedByAncestor.get(container);
      if (!g) {
        g = { kind: item.type, name: null, ancestor: container, members: [], ctx: item.ctx };
        groupedByAncestor.set(container, g);
        groups.push(g);
      }
      g.members.push(item.el);
      groupOf.set(item.el, g);
    }

    // Drop groups with only one member — they aren't really groups.
    for (const g of groups.slice()) {
      if (g.members.length < 2) {
        for (const m of g.members) groupOf.delete(m);
        continue;
      }
      firstMemberOf.set(g, g.members[0]);
    }

    return { groupOf, firstMemberOf, groups };
  }

  function collectChoiceDescendants(node) {
    let radios = 0, checkboxes = 0;
    let all;
    try { all = node.querySelectorAll('input, [role="radio"], [role="checkbox"]'); }
    catch (_) { return { total: 0, matchingKind: 0, otherKind: 0 }; }
    for (const el of all) {
      const kind = classifyControl(el);
      if (kind === "radio") radios++;
      else if (kind === "checkbox") checkboxes++;
    }
    return {
      total: radios + checkboxes,
      matchingKind: radios >= checkboxes ? radios : checkboxes,
      otherKind: radios >= checkboxes ? checkboxes : radios,
    };
  }

  function ancestorContainsOtherControls(node, exceptEl) {
    let controls;
    try { controls = node.querySelectorAll(NATIVE_SELECTOR + "," + ARIA_SELECTOR); }
    catch (_) { return false; }
    for (const c of controls) {
      if (c === exceptEl) continue;
      const t = classifyControl(c);
      if (t && t !== "radio" && t !== "checkbox") return true;
    }
    return false;
  }

  // ──────────── field building — single ────────────

  function buildSingleField(el, ctx, controlType) {
    const labelResult = extractLabel(el, ctx);
    const field = {
      selector: buildSelector(el),
      shadowPath: ctx.shadowPath.slice(),
      tag: el.tagName.toLowerCase(),
      type: controlType,
      name: el.getAttribute("name") || "",
      id: el.id || "",
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
      placeholder: el.getAttribute("placeholder") || "",
      aria_label: el.getAttribute("aria-label") || "",
      label: labelResult.text,
      label_source: labelResult.source,
      required: !!(el.required || el.getAttribute("aria-required") === "true"),
      current_value: readValue(el, controlType),
    };
    if (controlType === "select" || controlType === "select-multiple") {
      field.options = extractSelectOptions(el);
    } else if (controlType === "combobox") {
      field.options = extractComboboxOptions(el, ctx);
    }
    return field;
  }

  // ──────────── field building — groups ────────────

  function buildGroupField(group, _ctx) {
    const first = group.members[0];
    const ctx = group.ctx;
    const labelResult = extractGroupLabel(group, ctx);
    const options = group.members.map((el) => ({
      value: el.getAttribute("value") || "",
      text: optionText(el),
    }));
    const chosen = group.members.find((el) => isChecked(el));
    return {
      selector: buildGroupSelector(group),
      shadowPath: ctx.shadowPath.slice(),
      tag: "input",
      type: `${group.kind}_group`,
      name: group.name || "",
      id: "",
      autocomplete: "",
      placeholder: "",
      aria_label: (group.ancestor && group.ancestor.getAttribute("aria-label")) || "",
      label: labelResult.text,
      label_source: labelResult.source,
      required: !!(first.required || first.getAttribute("aria-required") === "true"),
      current_value: chosen ? (chosen.getAttribute("value") || optionText(chosen) || "on") : "",
      options,
      group_size: group.members.length,
    };
  }

  function buildGroupSelector(group) {
    if (group.name) {
      // Type-scoped for native inputs. ARIA radio buttons don't have
      // `type` and are hunted by role instead.
      if (group.kind === "radio" || group.kind === "checkbox") {
        return `input[type="${group.kind}"][name="${cssEscape(group.name)}"]`;
      }
    }
    if (group.ancestor) {
      const ancSel = elementPath(group.ancestor);
      const roleSel = group.kind === "radio" ? '[role="radio"]' : '[role="checkbox"]';
      return `${ancSel} input[type="${group.kind}"], ${ancSel} ${roleSel}`;
    }
    return elementPath(group.members[0]);
  }

  // ──────────── label extraction ────────────

  function extractLabel(el, ctx) {
    const root = ctx.root;
    const strategies = [
      () => tryLabelFor(el, root),
      () => tryWrappingLabel(el),
      () => tryAriaLabelledby(el, root),
      () => tryAriaLabelAttr(el),
      () => tryFieldsetLegend(el),
      () => tryQuestionContainer(el),
      () => tryPlaceholder(el),
      () => tryNameAttr(el),
    ];
    for (const s of strategies) {
      const r = s();
      if (r && r.text && r.text.trim()) return r;
    }
    return { text: "", source: "none" };
  }

  function extractGroupLabel(group, ctx) {
    const first = group.members[0];
    const anc = group.ancestor;

    // <fieldset><legend>...</legend>
    const fs = closestInSameContext(first, ctx, "fieldset");
    if (fs) {
      const legend = fs.querySelector("legend");
      if (legend) {
        const t = textOf(legend);
        if (t) return { text: t, source: "fieldset-legend" };
      }
    }
    // aria-labelledby / aria-label on the group container.
    if (anc) {
      const lb = tryAriaLabelledby(anc, ctx.root);
      if (lb && lb.text) return { text: lb.text, source: "group-aria-labelledby" };
      const al = (anc.getAttribute("aria-label") || "").trim();
      if (al) return { text: al, source: "group-aria-label" };
    }
    // Nearest question container starting from the group's ancestor
    // (or the first member's parent).
    const start = anc || first.parentElement;
    if (start) {
      const q = tryQuestionContainerFrom(start, group.members);
      if (q) return q;
    }
    return { text: "", source: "none" };
  }

  function tryLabelFor(el, root) {
    if (!el.id) return null;
    let lbl;
    try { lbl = root.querySelector(`label[for="${cssEscape(el.id)}"]`); }
    catch (_) { lbl = null; }
    return lbl ? { text: textOf(lbl), source: "label[for]" } : null;
  }

  function tryWrappingLabel(el) {
    let p = el.parentElement;
    while (p) {
      if (p.tagName === "LABEL") return { text: textOf(p), source: "wrapping-label" };
      p = p.parentElement;
    }
    return null;
  }

  function tryAriaLabelledby(el, root) {
    const ids = (el.getAttribute("aria-labelledby") || "").trim();
    if (!ids) return null;
    const texts = [];
    for (const id of ids.split(/\s+/)) {
      const ref = (root.getElementById && root.getElementById(id)) ||
                  (() => { try { return root.querySelector(`#${cssEscape(id)}`); } catch (_) { return null; } })();
      if (ref) texts.push(textOf(ref));
    }
    const joined = texts.join(" ").trim();
    return joined ? { text: joined, source: "aria-labelledby" } : null;
  }

  function tryAriaLabelAttr(el) {
    const v = (el.getAttribute("aria-label") || "").trim();
    return v ? { text: v, source: "aria-label" } : null;
  }

  function tryFieldsetLegend(el) {
    let node = el.parentElement;
    while (node) {
      if (node.tagName === "FIELDSET") {
        const lg = node.querySelector("legend");
        if (lg) {
          const t = textOf(lg);
          if (t) return { text: t, source: "fieldset-legend" };
        }
        break;
      }
      node = node.parentElement;
    }
    return null;
  }

  function tryPlaceholder(el) {
    const v = (el.getAttribute("placeholder") || "").trim();
    return v ? { text: v, source: "placeholder" } : null;
  }

  function tryNameAttr(el) {
    const v = (el.getAttribute("name") || "").trim();
    if (!v) return null;
    return { text: v.replace(/[_-]+/g, " "), source: "name-attr" };
  }

  // Generic "nearest question container". Walk up ancestors; at each
  // level check whether this element has a text-bearing sibling or
  // descendant that isn't itself a form control. Stop as soon as the
  // ancestor also contains OTHER controls beyond this one (which
  // means the container is too broad and would swallow the neighbour
  // question).
  function tryQuestionContainer(el) {
    let node = el.parentElement;
    let steps = 0;
    while (node && steps < 8) {
      // If this ancestor already contains other unrelated controls,
      // we've walked too far — the question container is finer than
      // this level and any text here would belong to a neighbour.
      if (ancestorContainsOtherControls(node, el)) break;
      const text = firstMeaningfulText(node, [el]);
      if (text) return { text, source: "question-container" };
      node = node.parentElement;
      steps++;
    }
    return null;
  }

  // Same idea as tryQuestionContainer but starting from a supplied
  // ancestor (used by radio/checkbox groups).
  function tryQuestionContainerFrom(startNode, excludeEls) {
    let node = startNode;
    let steps = 0;
    while (node && steps < 8) {
      if (ancestorContainsOtherControls(node, excludeEls[0])) break;
      const text = firstMeaningfulText(node, excludeEls);
      if (text) return { text, source: "question-container" };
      node = node.parentElement;
      steps++;
    }
    return null;
  }

  // Look for the first meaningful heading / label / short text-bearing
  // element inside `node` that isn't the excluded element or one of
  // its descendants. "Meaningful" = non-empty after trim + not just
  // punctuation and not a form-control tag.
  function firstMeaningfulText(node, excludes) {
    const excludeSet = new Set(excludes);
    // Prefer explicit label-like tags first.
    const preferred = "legend, label, h1, h2, h3, h4, h5, h6";
    let hits;
    try { hits = Array.from(node.querySelectorAll(preferred)); }
    catch (_) { hits = []; }
    for (const h of hits) {
      if (excludeSet.has(h)) continue;
      if (isDescendantOfAny(h, excludeSet)) continue;
      if (containsExcluded(h, excludeSet)) continue;
      const t = textOf(h);
      if (t) return t;
    }
    // Fallback — direct text children of `node` (skips wrapper divs).
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === 3 /* text */) {
        const t = (child.textContent || "").trim();
        if (t.length >= 2) return t;
      } else if (child.nodeType === 1 /* element */) {
        if (excludeSet.has(child)) continue;
        if (containsExcluded(child, excludeSet)) continue;
        if (isFormControl(child)) continue;
        const t = textOf(child);
        // Stop if the "text" is really an inline wrapper spanning
        // several controls — those tend to be long. Cap at 200 chars
        // so a full form paragraph doesn't become a label.
        if (t && t.length <= 200) return t;
      }
    }
    return "";
  }

  function isDescendantOfAny(node, set) {
    for (const s of set) {
      if (s !== node && s.contains && s.contains(node)) return true;
    }
    return false;
  }
  function containsExcluded(node, set) {
    for (const s of set) {
      if (s !== node && node.contains && node.contains(s)) return true;
    }
    return false;
  }
  function isFormControl(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    return role === "textbox" || role === "combobox" || role === "listbox" ||
           role === "radio" || role === "checkbox" || role === "switch";
  }

  // ──────────── option extraction ────────────

  function extractSelectOptions(sel) {
    return Array.from(sel.options)
      .filter((o) => !o.disabled)
      .filter((o) => (o.value || "").trim() !== "" ||
                     !/^(\s|-|—)*(select|choose|pick|please\s*select)/i
                       .test((o.textContent || "").trim()))
      .map((o) => ({ value: o.value, text: (o.textContent || "").trim() }));
  }

  function extractComboboxOptions(el, ctx) {
    // Try aria-controls / aria-owns → element with role="listbox"
    // → its role="option" descendants.
    const targetIds = ((el.getAttribute("aria-controls") || "") + " " +
                       (el.getAttribute("aria-owns") || "")).trim().split(/\s+/).filter(Boolean);
    const listboxes = [];
    for (const id of targetIds) {
      try {
        const t = ctx.root.getElementById
          ? ctx.root.getElementById(id)
          : ctx.root.querySelector(`#${cssEscape(id)}`);
        if (t) listboxes.push(t);
      } catch (_) { /* ignore */ }
    }
    // Fallback — look for a nearby listbox by role, but bounded to
    // the closest common ancestor to avoid grabbing an unrelated list.
    if (listboxes.length === 0) {
      const p = el.closest('[role="combobox"]')?.parentElement || el.parentElement;
      if (p) {
        try {
          const lb = p.querySelector('[role="listbox"]');
          if (lb) listboxes.push(lb);
        } catch (_) { /* ignore */ }
      }
    }
    const opts = [];
    const seen = new Set();
    for (const lb of listboxes) {
      let items;
      try { items = lb.querySelectorAll('[role="option"]'); }
      catch (_) { continue; }
      for (const it of items) {
        const val = it.getAttribute("data-value") || it.getAttribute("value") || (it.textContent || "").trim();
        const text = (it.textContent || "").trim();
        const key = `${val}::${text}`;
        if (seen.has(key)) continue;
        seen.add(key);
        opts.push({ value: val, text });
      }
    }
    return opts; // Empty is OK — the answering layer treats it as free-text.
  }

  // ──────────── value read / helpers ────────────

  function readValue(el, controlType) {
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      return opt ? opt.value : "";
    }
    if (controlType === "radio" || controlType === "checkbox" ||
        el.type === "radio" || el.type === "checkbox") {
      return isChecked(el) ? (el.getAttribute("value") || "on") : "";
    }
    // ARIA text/combobox — value may live in aria-activedescendant or
    // inner text. Return .value if present, otherwise textContent.
    if (typeof el.value === "string") return el.value;
    return (el.textContent || "").trim();
  }

  function isChecked(el) {
    if (typeof el.checked === "boolean") return el.checked;
    return el.getAttribute("aria-checked") === "true";
  }

  function optionText(inputEl) {
    // Wrapping label > label[for] > aria-label > value.
    let p = inputEl.parentElement;
    while (p) {
      if (p.tagName === "LABEL") return textOf(p);
      p = p.parentElement;
    }
    if (inputEl.id) {
      try {
        const lbl = (inputEl.ownerDocument || document).querySelector(
          `label[for="${cssEscape(inputEl.id)}"]`
        );
        if (lbl) return textOf(lbl);
      } catch (_) { /* ignore */ }
    }
    return (inputEl.getAttribute("aria-label") ||
            inputEl.getAttribute("value") ||
            (inputEl.textContent || "").trim());
  }

  function textOf(el) {
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function closestInSameContext(el, ctx, selector) {
    // .closest() stops naturally at the shadow-root boundary, which is
    // what we want — labels in the light DOM cannot label shadow-DOM
    // controls without an explicit ID reference.
    try { return el.closest(selector); }
    catch (_) { return null; }
  }

  // ──────────── selectors ────────────

  function buildSelector(el) {
    if (el.id) return `#${cssEscape(el.id)}`;
    const name = el.getAttribute("name");
    if (name) {
      const form = el.closest && el.closest("form");
      if (form && form.getAttribute("name")) {
        return `form[name="${cssEscape(form.getAttribute("name"))}"] [name="${cssEscape(name)}"]`;
      }
      return `${el.tagName.toLowerCase()}[name="${cssEscape(name)}"]`;
    }
    const testid = el.getAttribute("data-testid") ||
                   el.getAttribute("data-test-id") ||
                   el.getAttribute("data-automation-id");
    if (testid) return `${el.tagName.toLowerCase()}[data-testid="${cssEscape(testid)}"]`;
    return elementPath(el);
  }

  function elementPath(el) {
    const parts = [];
    let node = el;
    const stopAt = (el.getRootNode && el.getRootNode()) || document;
    const body = stopAt.body || stopAt;
    while (node && node.nodeType === 1 && node !== body) {
      const parent = node.parentElement || (node.parentNode && node.parentNode.host);
      if (!parent) break;
      const same = Array.from(parent.children || []).filter(
        (c) => c.tagName === node.tagName
      );
      const idx = same.indexOf(node) + 1;
      parts.unshift(`${node.tagName.toLowerCase()}:nth-of-type(${idx})`);
      if (parent.tagName === "FORM") { parts.unshift("form"); break; }
      node = parent;
    }
    return parts.join(" > ");
  }

  function cssEscape(s) {
    if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(s);
    return String(s).replace(/([^\w-])/g, "\\$1");
  }

  // ──────────── dedupe ────────────

  function dedupeFields(fields) {
    // Identity: (shadowPath, selector, type). Same label with distinct
    // selectors survives so "Email" and "Confirm Email" stay separate.
    const seen = new Set();
    const out = [];
    for (const f of fields) {
      const key = `${(f.shadowPath || []).join("|")}::${f.selector}::${f.type}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(f);
    }
    return out;
  }

  // ──────────── fill ────────────

  function fillField(selector, value, shadowPath) {
    const scope = resolveShadowScope(shadowPath || []);
    if (!scope) return { ok: false, error: "shadowPath did not resolve" };

    let matches;
    try { matches = Array.from(scope.querySelectorAll(selector)); }
    catch (e) { return { ok: false, error: `Bad selector: ${e.message}` }; }
    if (matches.length === 0) return { ok: false, error: `No element for selector: ${selector}` };

    // Choice group: multiple radios/checkboxes returned — pick by value/text.
    if (matches.length > 1 && matches.every(isChoiceControl)) {
      const want = String(value).trim().toLowerCase();
      const pick =
        matches.find((el) => (el.getAttribute("value") || "").trim().toLowerCase() === want) ||
        matches.find((el) => optionText(el).trim().toLowerCase() === want) ||
        matches.find((el) => optionText(el).trim().toLowerCase().includes(want)) ||
        matches.find((el) => want.includes(optionText(el).trim().toLowerCase()));
      if (!pick) return { ok: false, error: `No option in group matches '${value}'.` };
      if (pick.disabled) return { ok: false, error: "Chosen option is disabled." };
      const kind = classifyControl(pick);
      if (kind === "radio") {
        for (const el of matches) if (el !== pick) setChecked(el, false);
      }
      setChecked(pick, true);
      pick.dispatchEvent(new Event("input", { bubbles: true }));
      pick.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, filled: optionText(pick) || pick.getAttribute("value") || "on" };
    }

    const el = matches[0];
    if (el.disabled) return { ok: false, error: "Field is disabled." };

    try {
      const kind = classifyControl(el);
      if (el.tagName === "SELECT") {
        const match = Array.from(el.options).find(
          (o) => o.value === value ||
                 (o.textContent || "").trim().toLowerCase() === String(value).toLowerCase()
        );
        if (!match) return { ok: false, error: `No option matches '${value}'.` };
        el.value = match.value;
      } else if (kind === "checkbox" || kind === "radio") {
        const on = String(value).toLowerCase();
        const desired = ["true", "yes", "on", "1", (el.getAttribute("value") || "").toLowerCase()].includes(on);
        setChecked(el, desired);
      } else if (kind === "combobox") {
        // Custom combobox — best-effort. Try nested <input> first
        // (most React comboboxes have one under the hood), else set
        // aria-activedescendant + textContent as a hint.
        const nested = el.querySelector("input");
        if (nested) {
          writeInputValue(nested, value);
        } else {
          try { el.textContent = String(value); } catch (_) { /* ignore */ }
        }
      } else {
        writeInputValue(el, value);
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, filled: readValue(el, kind || "text") };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  }

  function writeInputValue(el, value) {
    if (typeof el.focus === "function") el.focus();
    const proto = el.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  function setChecked(el, on) {
    if (typeof el.checked === "boolean") {
      el.checked = !!on;
    } else {
      el.setAttribute("aria-checked", on ? "true" : "false");
    }
  }

  function isChoiceControl(el) {
    const k = classifyControl(el);
    return k === "radio" || k === "checkbox";
  }

  function resolveShadowScope(shadowPath) {
    if (!shadowPath || shadowPath.length === 0) return document;
    let scope = document;
    for (const step of shadowPath) {
      let host;
      try { host = scope.querySelector(step); }
      catch (_) { return null; }
      if (!host || !host.shadowRoot) return null;
      scope = host.shadowRoot;
    }
    return scope;
  }

  // ──────────── exports ────────────

  root.detectFormFields = detectFormFields;
  root.fillField = fillField;
})(typeof globalThis !== "undefined" ? globalThis : window);
