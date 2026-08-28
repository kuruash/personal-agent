// Phase 4 form detection + write.
// Read-only detectFormFields() scans the DOM for form controls and returns a
// stable description of each one (selector, label, autocomplete, name/id,
// placeholder, options). Write-side fillField(selector, value) sets the value
// on a single control (or picks the right member of a radio group) and
// dispatches input/change events so React/Vue/etc. notice. Never submits —
// submission stays a manual user action.
//
// Radio and checkbox groups are collapsed: one field per group (keyed on
// shared `name` attribute, falling back to closest fieldset/[role=radiogroup]
// ancestor), with the group's question text as the label and each option's
// {value, text} attached under `options`. The individual member inputs are
// NOT emitted as separate fields.
//
// Also sourceable from extension/fixtures/formtest.html and formstest.html
// for offline harness testing.
//
// Load-bearing assumptions (also noted in CLAUDE.md):
//   - Choice groups: shared `name` attr, or shared fieldset / role=radiogroup
//     ancestor, or (rescue pass) two-or-more choice inputs sharing the
//     nearest FALLBACK_GROUP_ANCESTOR_SELECTORS ancestor. The rescue pass
//     is what handles live Microsoft Forms, whose per-question wrappers
//     have no ARIA role and whose radios each carry a distinct auto-
//     generated name. Everything else (button-typed inputs, hidden,
//     disabled) is dropped as before.
//   - Label association per input: label[for], wrapping label, row-label,
//     aria-labelledby, then FALLBACK_LABEL_SELECTORS ancestor walk. For
//     groups: legend, group-level aria attrs, then the same fallback walk.
//   - Fallback walk prefers the DEEPEST matching descendant at each ancestor
//     level, to avoid picking a wrapper whose textContent concatenates the
//     question with sibling hint elements.
//   - Selector generation prefers #id, then form[name]+[name]. For groups
//     with a shared name we emit a group selector like
//     input[name="X"][type="radio"], and fillField picks the right member.

(function (root) {
  const SKIP_INPUT_TYPES = new Set([
    "submit", "button", "reset", "image", "hidden", "file", "password",
  ]);

  // Site-specific "question text lives here" containers. Ordered by
  // preference (first match in DOM-ascent wins). Each entry is a CSS
  // selector searched within ancestors of the input when standard
  // heuristics come up dry or return a generic type-hint. Add one line per
  // new site — no new logic needed.
  const FALLBACK_LABEL_SELECTORS = [
    // Microsoft Forms: question rendered in a rich-text div nowhere near
    // the input by DOM standards, only reachable via a shared ancestor.
    '[class*="text-format-content"]',
  ];

  // Site-specific "everything for ONE question lives in here" containers,
  // used to group choice inputs (radio/checkbox) that DON'T share a `name`
  // attribute AND aren't inside a <fieldset> or role="radiogroup". Ordered
  // by preference. Add one line per new site.
  //
  // On live Microsoft Forms the radio/checkbox <input>s inside a single
  // question each carry their own generated name, and the ancestor wrapper
  // has no ARIA role — so the standard shared-name / shared-role grouping
  // paths both miss and each option leaks out as its own field. This
  // ancestor chain rescues that case.
  const FALLBACK_GROUP_ANCESTOR_SELECTORS = [
    '[data-automation-id="questionItem"]',
    '[data-automation-id*="question"]',
    '[class*="office-form-question"]',
    '[class*="question-item"]',
  ];

  // Labels that formally exist but tell us nothing about what the field is
  // asking for (they describe the field TYPE, not the QUESTION). Treated as
  // if empty so the fallback chain fires; also STRIPPED from any label text
  // returned by textOf() so nested wrappers that concatenate a real question
  // with these hints ("Single line text.") come out clean.
  const GENERIC_LABELS = [
    "single line text",
    "enter your answer",
    "your answer",
    "type your answer",
  ];
  const GENERIC_LABELS_SET = new Set(GENERIC_LABELS);

  function detectFormFields(scope) {
    scope = scope || document;
    const nodes = Array.from(scope.querySelectorAll("input, select, textarea"));

    // 1. Collapse radios/checkboxes into groups. Members are skipped in the
    //    per-input walk below; groups are emitted at the position of their
    //    first member so document-order is preserved.
    const { groupOf, firstMemberOf } = collapseChoiceGroups(nodes, scope);
    const emittedGroups = new Set();
    const out = [];

    for (const el of nodes) {
      if (el.tagName === "INPUT" && SKIP_INPUT_TYPES.has((el.type || "").toLowerCase())) {
        continue;
      }
      if (el.disabled) continue;

      const grp = groupOf.get(el);
      if (grp) {
        // Emit the group once, at the position of its first member.
        if (emittedGroups.has(grp)) continue;
        if (firstMemberOf.get(grp) !== el) continue;
        emittedGroups.add(grp);
        out.push(buildGroupField(grp, scope));
        continue;
      }

      out.push(buildSingleField(el, scope));
    }
    try {
      console.log(
        "[FORM DEBUG] detectFormFields result:",
        "frame=", (typeof window !== "undefined" && window === window.top) ? "TOP" : "CHILD",
        "url=", (typeof location !== "undefined" ? location.href : "?"),
        "count=", out.length,
        out
      );
    } catch (_) {}
    return out;
  }

  // ---------- single-input fields ----------

  function buildSingleField(el, scope) {
    const labelResult = findLabel(el, scope);
    const field = {
      selector: buildSelector(el),
      tag: el.tagName.toLowerCase(),
      type: (el.type || "").toLowerCase(),
      name: el.getAttribute("name") || "",
      id: el.id || "",
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
      placeholder: el.getAttribute("placeholder") || "",
      aria_label: el.getAttribute("aria-label") || "",
      label: labelResult.text,
      label_source: labelResult.source,
      required: !!el.required,
      current_value: readValue(el),
    };
    if (el.tagName === "SELECT") {
      // Normalize into the same {value, text}[] shape radio_group uses.
      // Filter out disabled options and the "-- select --" style placeholder
      // (empty value; it's not a real answer, it's a "please pick one" hint).
      // Keep the placeholder-text option ONLY if it has a non-empty value so
      // sites that use "prefer_not_say" style empty values still round-trip.
      field.options = Array.from(el.options)
        .filter((o) => !o.disabled)
        .filter((o) => (o.value || "").trim() !== "" ||
                       !/^(\s|-|—)*(select|choose|pick|please\s*select)(\s|-|—|\.|:)*(one|an option|.*)?\s*$/i
                         .test((o.textContent || "").trim()))
        .map((o) => ({
          value: o.value,
          text: (o.textContent || "").trim(),
        }));
      // Multi-select gets its own type so the resolver can render checkboxes.
      if (el.multiple) field.type = "select-multiple";
    }
    return field;
  }

  // ---------- radio/checkbox group fields ----------

  function collapseChoiceGroups(nodes, scope) {
    // Returns {groupOf: Map<el,group>, firstMemberOf: Map<group,el>}.
    // A group is {kind: "radio"|"checkbox", name: string|null,
    //             ancestor: Element|null, members: Element[]}.
    const groups = [];
    const byName = new Map();       // name → group
    const byAncestor = new Map();   // fieldset/radiogroup element → group
    const groupOf = new Map();
    const firstMemberOf = new Map();

    for (const el of nodes) {
      if (el.tagName !== "INPUT" || el.disabled) continue;
      const t = (el.type || "").toLowerCase();
      if (t !== "radio" && t !== "checkbox") continue;

      const name = el.getAttribute("name");
      let group;
      if (name) {
        const key = `${t}:${name}`;
        group = byName.get(key);
        if (!group) {
          group = { kind: t, name, ancestor: null, members: [] };
          byName.set(key, group);
          groups.push(group);
        }
      } else {
        // Group by nearest [role=radiogroup] or <fieldset> ancestor. If
        // neither exists this input is truly standalone; treat as a singleton
        // by NOT recording it in groupOf, so buildSingleField handles it.
        const anc = el.closest('[role="radiogroup"], fieldset');
        if (!anc) continue;
        group = byAncestor.get(anc);
        if (!group) {
          group = { kind: t, name: null, ancestor: anc, members: [] };
          byAncestor.set(anc, group);
          groups.push(group);
        }
      }
      group.members.push(el);
      groupOf.set(el, group);
    }

    // Second pass: rescue MS-Forms-style choice inputs that got here as
    // singletons because they each have their own unique `name` and no
    // fieldset/radiogroup ancestor. Group by nearest matching per-question
    // wrapper ancestor.
    //
    // We do this AFTER the standard passes so shared-name radios (e.g. a
    // classic Yes/No radio) always win over ancestor-based collapse.
    // Nested map: ancestor element → (kind → bucket). Element identity as
    // outer key sidesteps needing a stringified selector.
    const byFallbackAncestor = new Map();
    const fallbackBuckets = [];
    for (const el of nodes) {
      if (el.tagName !== "INPUT" || el.disabled) continue;
      const t = (el.type || "").toLowerCase();
      if (t !== "radio" && t !== "checkbox") continue;
      // Already in a real (multi-member) group → leave alone. Singleton
      // "groups" (size 1) are still eligible for rescue here.
      const existing = groupOf.get(el);
      if (existing && existing.members.length >= 2) continue;

      let anc = null;
      for (const sel of FALLBACK_GROUP_ANCESTOR_SELECTORS) {
        try { anc = el.closest(sel); } catch { anc = null; }
        if (anc) break;
      }
      if (!anc) continue;
      let byKind = byFallbackAncestor.get(anc);
      if (!byKind) {
        byKind = new Map();
        byFallbackAncestor.set(anc, byKind);
      }
      let bucket = byKind.get(t);
      if (!bucket) {
        bucket = { kind: t, ancestor: anc, members: [] };
        byKind.set(t, bucket);
        fallbackBuckets.push(bucket);
      }
      bucket.members.push(el);
    }
    for (const bucket of fallbackBuckets) {
      if (bucket.members.length < 2) continue;
      // Detach any prior singleton-group memberships and adopt these members
      // into a new ancestor-based group.
      for (const m of bucket.members) {
        const prior = groupOf.get(m);
        if (prior) {
          prior.members = prior.members.filter((x) => x !== m);
        }
      }
      const group = {
        kind: bucket.kind,
        name: null,
        ancestor: bucket.ancestor,
        members: bucket.members,
      };
      groups.push(group);
      for (const m of group.members) groupOf.set(m, group);
    }

    // A "group" of size 1 isn't really a group — let it fall back to
    // single-input handling so we don't wrap a lonely checkbox in a
    // radiogroup-style plan.
    for (const group of groups) {
      if (group.members.length < 2) {
        for (const m of group.members) groupOf.delete(m);
        continue;
      }
      firstMemberOf.set(group, group.members[0]);
    }
    return { groupOf, firstMemberOf };
  }

  function buildGroupField(group, scope) {
    const first = group.members[0];
    const labelResult = findGroupLabel(group, scope);
    const options = group.members.map((el) => ({
      value: el.value || "",
      text: optionText(el),
    }));
    const anyChecked = group.members.find((el) => el.checked);
    return {
      selector: buildGroupSelector(group),
      tag: "input",
      type: group.kind + "_group",
      name: group.name || "",
      id: "",
      autocomplete: "",
      placeholder: "",
      aria_label: (group.ancestor && group.ancestor.getAttribute("aria-label")) || "",
      label: labelResult.text,
      label_source: labelResult.source,
      required: !!first.required,
      current_value: anyChecked ? (anyChecked.value || "") : "",
      options,
      // How many members were collapsed — handy in traces.
      group_size: group.members.length,
    };
  }

  function buildGroupSelector(group) {
    if (group.name) {
      // Escaping same rules as buildSelector. Type-scoped so
      // `document.querySelectorAll` in fillField only sees group members.
      return `input[type="${group.kind}"][name="${cssEscape(group.name)}"]`;
    }
    // Ancestor-based fallback: a selector that targets all inputs of this
    // kind inside the specific ancestor. We use nth-of-type on the ancestor
    // relative to its parent to keep it stable across similar groups.
    const anc = group.ancestor;
    const ancSel = elementPath(anc);
    return `${ancSel} input[type="${group.kind}"]`;
  }

  function optionText(inputEl) {
    // Prefer <label> that wraps the input (Microsoft Forms pattern), then
    // <label for=id>, then the input's own aria-label / value.
    let p = inputEl.parentElement;
    while (p) {
      if (p.tagName === "LABEL") return textOf(p);
      p = p.parentElement;
    }
    if (inputEl.id) {
      const lbl = inputEl.ownerDocument.querySelector(
        `label[for="${cssEscape(inputEl.id)}"]`
      );
      if (lbl) return textOf(lbl);
    }
    return (inputEl.getAttribute("aria-label") || inputEl.value || "").trim();
  }

  function findGroupLabel(group, scope) {
    // Strategies specific to a group of inputs. Falls through to the
    // per-input findLabel on the first member as a last resort, but that's
    // usually the option text — undesirable — so we exhaust group-scoped
    // strategies first.
    const anc = group.ancestor;
    const first = group.members[0];

    // 1. <fieldset><legend>...</legend>...</fieldset>
    const fs = first.closest("fieldset");
    if (fs) {
      const legend = fs.querySelector("legend");
      if (legend) {
        const t = textOf(legend);
        if (t && !isGeneric(t)) return { text: t, source: "fieldset-legend" };
      }
    }

    // 2. aria-labelledby / aria-label on the [role=radiogroup] container.
    if (anc) {
      const ids = (anc.getAttribute("aria-labelledby") || "").trim();
      if (ids) {
        const texts = [];
        for (const id of ids.split(/\s+/)) {
          const ref = scope.getElementById
            ? scope.getElementById(id)
            : scope.querySelector(`#${cssEscape(id)}`);
          if (ref) texts.push(textOf(ref));
        }
        const joined = texts.join(" ").trim();
        if (joined && !isGeneric(joined)) return { text: joined, source: "group-aria-labelledby" };
      }
      const al = (anc.getAttribute("aria-label") || "").trim();
      if (al && !isGeneric(al)) return { text: al, source: "group-aria-label" };
    }

    // 3. Fallback selectors: walk up from the group's common ancestor
    //    (or the first radio's parent if no explicit group container).
    const startFrom = anc ? anc.parentElement : first.parentElement;
    const fbFrom = { parentElement: startFrom, contains: (n) => group.members.some((m) => m === n || (startFrom && startFrom.contains(m))) };
    // Reuse tryFallbackSelectors but pass a synthetic anchor whose ancestor
    // walk starts at the group's own ancestor. Simpler: just inline the walk.
    let node = startFrom;
    while (node && node !== scope.body && node !== scope) {
      const found = findLeafFallbackInside(node, group.members);
      if (found) return found;
      node = node.parentElement;
    }
    return { text: "", source: "none" };
  }

  // ---------- shared label helpers ----------

  function findLabel(el, scope) {
    const strategies = [
      () => tryLabelFor(el, scope),
      () => tryWrappingLabel(el, scope),
      () => tryRowLabel(el, scope),
      () => tryAriaLabelledby(el, scope),
      () => tryFallbackSelectors(el, scope),
    ];
    for (const s of strategies) {
      const r = s();
      if (r && r.text && !isGeneric(r.text)) return r;
    }
    return { text: "", source: "none" };
  }

  function tryLabelFor(el, scope) {
    if (!el.id) return null;
    const lbl = scope.querySelector(`label[for="${cssEscape(el.id)}"]`);
    return lbl ? { text: textOf(lbl), source: "label[for]" } : null;
  }

  function tryWrappingLabel(el, scope) {
    let p = el.parentElement;
    while (p && p !== scope.body && p !== scope) {
      if (p.tagName === "LABEL") return { text: textOf(p), source: "wrapping-label" };
      p = p.parentElement;
    }
    return null;
  }

  function tryRowLabel(el, scope) {
    const row = el.parentElement;
    if (!row) return null;
    const lbl = row.querySelector("label");
    if (lbl && !lbl.contains(el)) return { text: textOf(lbl), source: "row-label" };
    return null;
  }

  function tryAriaLabelledby(el, scope) {
    const ids = (el.getAttribute("aria-labelledby") || "").trim();
    if (!ids) return null;
    const texts = [];
    for (const id of ids.split(/\s+/)) {
      const ref = scope.getElementById
        ? scope.getElementById(id)
        : scope.querySelector(`#${cssEscape(id)}`);
      if (ref) texts.push(textOf(ref));
    }
    const joined = texts.join(" ").trim();
    return joined ? { text: joined, source: "aria-labelledby" } : null;
  }

  function tryFallbackSelectors(el, scope) {
    // Walk up ancestors; at each level, find the DEEPEST matching descendant
    // that doesn't itself contain a matching descendant AND doesn't contain
    // the input. Deepest-wins is what fixes the "outer wrapper concatenates
    // question + hint spans" bug — the outer element and its inner text-
    // format-content div both match the class selector, but only the inner
    // one contains just the question.
    let node = el.parentElement;
    while (node && node !== scope.body && node !== scope) {
      const found = findLeafFallbackInside(node, [el]);
      if (found) return found;
      node = node.parentElement;
    }
    return null;
  }

  function findLeafFallbackInside(node, excludeElements) {
    for (const sel of FALLBACK_LABEL_SELECTORS) {
      let hits;
      try {
        hits = Array.from(node.querySelectorAll(sel));
      } catch { continue; }
      if (hits.length === 0) continue;

      // Filter out hits that contain any of the excluded inputs — those
      // aren't LABEL containers, they're WRAPPER containers.
      hits = hits.filter((h) => !excludeElements.some((e) => h.contains(e)));
      if (hits.length === 0) continue;

      // Pick a leaf: one that doesn't contain any of the other hits.
      let leaf = null;
      for (const h of hits) {
        const hasChildHit = hits.some((o) => o !== h && h.contains(o));
        if (!hasChildHit) { leaf = h; break; }
      }
      leaf = leaf || hits[0];

      const text = textOf(leaf);
      if (text) return { text, source: `fallback:${sel}` };
    }
    return null;
  }

  function isGeneric(text) {
    return GENERIC_LABELS_SET.has(text.trim().toLowerCase());
  }

  function textOf(el) {
    const raw = (el.textContent || "").replace(/\s+/g, " ").trim();
    // Defensive strip: if a returned label carries embedded type-hints
    // ("Single line text.") because its container aggregates several
    // sibling spans, remove them. Same set as GENERIC_LABELS, matched
    // case-insensitively. Preserves the real question text.
    let cleaned = raw;
    for (const phrase of GENERIC_LABELS) {
      const re = new RegExp(escapeRegex(phrase) + "\\.?", "gi");
      cleaned = cleaned.replace(re, " ");
    }
    return cleaned.replace(/\s+/g, " ").trim();
  }

  function escapeRegex(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function readValue(el) {
    if (el.tagName === "SELECT") {
      const opt = el.options[el.selectedIndex];
      return opt ? opt.value : "";
    }
    if (el.type === "checkbox" || el.type === "radio") {
      return el.checked ? (el.value || "on") : "";
    }
    return el.value || "";
  }

  function buildSelector(el) {
    if (el.id) return `#${cssEscape(el.id)}`;
    const name = el.getAttribute("name");
    if (name) {
      const form = el.closest("form");
      if (form && form.getAttribute("name")) {
        return `form[name="${cssEscape(form.getAttribute("name"))}"] [name="${cssEscape(name)}"]`;
      }
      return `${el.tagName.toLowerCase()}[name="${cssEscape(name)}"]`;
    }
    return elementPath(el);
  }

  function elementPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== node.ownerDocument.body) {
      const parent = node.parentElement;
      if (!parent) break;
      const same = Array.from(parent.children).filter(
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

  // ---------- write side ----------

  function fillField(selector, value) {
    let matches;
    try { matches = Array.from(document.querySelectorAll(selector)); }
    catch (e) { return { ok: false, error: `Bad selector: ${e.message}` }; }
    if (matches.length === 0) return { ok: false, error: `No element for selector: ${selector}` };

    // Group case: multiple radios/checkboxes returned. Pick the member whose
    // value or associated option text matches.
    if (matches.length > 1 && matches.every((el) => el.tagName === "INPUT" &&
        (el.type === "radio" || el.type === "checkbox"))) {
      const want = String(value).trim().toLowerCase();
      const pick = matches.find((el) => (el.value || "").trim().toLowerCase() === want)
        || matches.find((el) => optionText(el).trim().toLowerCase() === want)
        || matches.find((el) => optionText(el).trim().toLowerCase().includes(want))
        || matches.find((el) => want.includes(optionText(el).trim().toLowerCase()));
      if (!pick) return { ok: false, error: `No option in group matches '${value}'.` };
      if (pick.disabled) return { ok: false, error: "Chosen option is disabled." };
      if (pick.type === "radio") {
        // Uncheck peers first (radios should auto-manage but React-controlled
        // groups can drift), then check the winner.
        for (const el of matches) if (el !== pick) el.checked = false;
      }
      pick.checked = true;
      pick.dispatchEvent(new Event("input", { bubbles: true }));
      pick.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, filled: optionText(pick) || pick.value };
    }

    const el = matches[0];
    if (el.disabled) return { ok: false, error: "Field is disabled." };
    try {
      if (el.tagName === "SELECT") {
        const match = Array.from(el.options).find(
          (o) => o.value === value ||
                 (o.textContent || "").trim().toLowerCase() === String(value).toLowerCase()
        );
        if (!match) return { ok: false, error: `No option matches '${value}'.` };
        el.value = match.value;
      } else if (el.type === "checkbox" || el.type === "radio") {
        const on = String(value).toLowerCase();
        el.checked = ["true", "yes", "on", "1", el.value?.toLowerCase()].includes(on);
      } else {
        el.focus();
        const proto = el.tagName === "TEXTAREA"
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
        if (setter) setter.call(el, value);
        else el.value = value;
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, filled: readValue(el) };
    } catch (e) {
      return { ok: false, error: String(e?.message ?? e) };
    }
  }

  root.detectFormFields = detectFormFields;
  root.fillField = fillField;
})(typeof globalThis !== "undefined" ? globalThis : window);
