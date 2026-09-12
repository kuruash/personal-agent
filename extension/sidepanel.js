console.log("[PA DEBUG] sidepanel loaded", new Date().toISOString());

const q = document.getElementById("q");
const askBtn = document.getElementById("ask");
const answerEl = document.getElementById("answer");
const statusEl = document.getElementById("status");

const draftArea = document.getElementById("draft-area");
const draftBody = document.getElementById("draft-body");
const draftError = document.getElementById("draft-error");
const insertBtn = document.getElementById("insert-btn");
const rejectBtn = document.getElementById("reject-btn");

const formArea = document.getElementById("form-area");
const formFields = document.getElementById("form-fields");
const approveAllBtn = document.getElementById("approve-all-btn");
const closeFormBtn = document.getElementById("close-form-btn");

// Tab the current form session was discovered on. Pinned by the
// backend's ASK response and echoed back on every FILL_FIELD so
// routing never depends on chrome.tabs.query — that query returns
// nothing when the side panel itself is focused.
let formSessionTabId = null;

function hideForm() {
  formArea.style.display = "none";
  formFields.innerHTML = "";
}

// Single fill routing path used by both manual Fill and auto-fill.
async function sendFillFromRow(row, controlEl, value) {
  const frameId = row.dataset.frameId ? Number(row.dataset.frameId) : undefined;
  const shadowPath = row.dataset.shadowPath ? JSON.parse(row.dataset.shadowPath) : [];
  console.log(
    "[FORM DEBUG][SESSION]",
    JSON.stringify({
      storedTabId: formSessionTabId,
      rowFrameId: frameId,
      hasShadowPath: shadowPath.length > 0,
    })
  );
  if (typeof formSessionTabId !== "number") {
    return { ok: false, error: "Form session tabId missing — press Cmd+Shift+F again." };
  }
  return await chrome.runtime.sendMessage({
    type: "FILL_FIELD",
    tabId: formSessionTabId,
    frameId,
    selector: controlEl.dataset.selector,
    value,
    shadowPath,
  });
}

// Two states only. Server sets `state`: "ready" (has answer) or "unknown"
// (empty — user needs to type). Choose textarea vs single-line based on
// answer length or the underlying field type.
function isMultiline(f) {
  const t = (f.type || "").toLowerCase();
  if (t === "textarea") return true;
  const v = f.value || "";
  return v.length > 80 || v.includes("\n");
}

function debugBlock(f) {
  return `
    <details class="debug">
      <summary>Details</summary>
      <div class="debug-body">
        <div><b>source:</b> ${escapeHtml(f.source || "")}</div>
        <div><b>selector:</b> <code>${escapeHtml(f.selector || "")}</code></div>
      </div>
    </details>
  `;
}

const STATE_LABEL = {
  ready: `<span class="state-ready">READY</span>`,
  unknown: `<span class="state-unknown">UNKNOWN</span>`,
  retrieving: `<span class="state-generate">RETRIEVING…</span>`,
  generating: `<span class="state-generate">GENERATING…</span>`,
};

function _placeholder_for(state) {
  if (state === "unknown") return "Enter a value";
  if (state === "retrieving") return "Retrieving evidence…";
  if (state === "generating") return "Generating answer…";
  return "";
}

function showForm(fields) {
  formFields.innerHTML = "";
  for (const [i, f] of fields.entries()) {
    const row = document.createElement("div");
    row.className = "field-row";
    // "ready" | "unknown" | "retrieving" | "generating"
    const state = f.state && STATE_LABEL[f.state] ? f.state : "unknown";
    row.dataset.state = state;
    if (typeof f.field_id === "number") row.dataset.fieldId = String(f.field_id);
    const label = f.label || f.selector || `field ${i + 1}`;
    const selectorAttr = escapeAttr(f.selector || "");
    const badge = STATE_LABEL[state];
    const value = f.value ?? "";
    const placeholder = _placeholder_for(state);
    const control = isMultiline(f)
      ? `<textarea data-selector="${selectorAttr}" rows="4"
                   placeholder="${escapeAttr(placeholder)}">${escapeHtml(value)}</textarea>`
      : `<input type="text" data-selector="${selectorAttr}"
                value="${escapeAttr(value)}"
                placeholder="${escapeAttr(placeholder)}" />`;
    row.innerHTML = `
      <div class="lbl">${escapeHtml(label)}</div>
      <div class="meta">${badge}</div>
      ${control}
      <div class="actions">
        <button class="fill-btn">Fill</button>
        <button class="skip-btn">Skip</button>
      </div>
      ${debugBlock(f)}
      <div class="status"></div>
    `;
    // Frame identity + shadow-DOM path: detection ran in a specific
    // frame (and possibly a shadow root inside that frame) and returned
    // both. Fill MUST be routed back to the same context or the
    // selector won't match. Store on the row so both handlers see it.
    if (typeof f.frameId === "number") row.dataset.frameId = String(f.frameId);
    if (Array.isArray(f.shadowPath) && f.shadowPath.length > 0) {
      row.dataset.shadowPath = JSON.stringify(f.shadowPath);
    }
    const control_el = row.querySelector("input, textarea");
    const fillBtn = row.querySelector(".fill-btn");
    const skipBtn = row.querySelector(".skip-btn");
    const rowStatus = row.querySelector(".status");
    fillBtn.addEventListener("click", async () => {
      const v = control_el.value;
      if (!v) {
        rowStatus.className = "status err";
        rowStatus.textContent = "Value is empty — nothing to fill.";
        return;
      }
      fillBtn.disabled = true;
      try {
        const resp = await sendFillFromRow(row, control_el, v);
        if (!resp?.ok) throw new Error(resp?.error ?? "Fill failed.");
        rowStatus.className = "status ok";
        rowStatus.textContent = `Filled: ${(resp.filled ?? v).slice(0, 80)}`;
      } catch (e) {
        rowStatus.className = "status err";
        rowStatus.textContent = String(e.message ?? e);
      } finally {
        fillBtn.disabled = false;
      }
    });
    skipBtn.addEventListener("click", () => {
      row.style.opacity = "0.5";
      fillBtn.disabled = true;
      skipBtn.disabled = true;
      rowStatus.className = "status";
      rowStatus.textContent = "Skipped.";
    });
    formFields.appendChild(row);
  }
  formArea.style.display = "block";
  autoFillReadyRows();
}


// ── Streaming patches ───────────────────────────────────────

function _rowByFieldId(fid) {
  return formFields.querySelector(`.field-row[data-field-id="${fid}"]`);
}

function _setBadge(row, state) {
  row.dataset.state = state;
  const badgeEl = row.querySelector(".meta");
  if (badgeEl) badgeEl.innerHTML = STATE_LABEL[state] || STATE_LABEL.unknown;
}

function patchRowFromField(f) {
  // f: full plan entry from server (may have state ready/unknown/retrieving/generating)
  const row = _rowByFieldId(f.field_id);
  if (!row) return;
  const state = f.state && STATE_LABEL[f.state] ? f.state : "unknown";
  _setBadge(row, state);
  // Update the editable control's value + placeholder.
  const control = row.querySelector("input, textarea");
  if (control) {
    if (f.value != null && String(f.value) !== "") {
      control.value = String(f.value);
    }
    const ph = _placeholder_for(state);
    if (ph) control.placeholder = ph;
    else control.placeholder = "";
  }
  // Refresh the debug source line if the details block is present.
  const dbg = row.querySelector("details.debug .debug-body");
  if (dbg && f.source) {
    const srcLine = dbg.querySelector("div:first-child");
    if (srcLine) srcLine.innerHTML = `<b>source:</b> ${escapeHtml(f.source)}`
      + (typeof f.latency_ms === "number" ? ` <i>(${f.latency_ms.toFixed(0)} ms)</i>` : "");
  }
  // If a semantic field arrived READY, auto-fill it now (same safety
  // rules as direct auto-fill: value present, options-fit already
  // enforced server-side).
  if (state === "ready" && !row.dataset.autofilled) {
    autoFillSingleRow(row);
  }
}

function autoFillSingleRow(row) {
  if (row.dataset.autofilled === "1") return;
  const control = row.querySelector("input, textarea");
  const fillBtn = row.querySelector(".fill-btn");
  const rowStatus = row.querySelector(".status");
  if (!control?.value || !fillBtn || fillBtn.disabled) return;
  row.dataset.autofilled = "1";
  fillBtn.disabled = true;
  (async () => {
    try {
      const resp = await sendFillFromRow(row, control, control.value);
      if (!resp?.ok) throw new Error(resp?.error ?? "Auto-fill failed.");
      rowStatus.className = "status ok";
      rowStatus.textContent = `Auto-filled: ${(resp.filled ?? control.value).slice(0, 80)}`;
    } catch (e) {
      rowStatus.className = "status err";
      rowStatus.textContent = `Auto-fill: ${e.message ?? e}`;
    } finally {
      fillBtn.disabled = false;
    }
  })();
}

function handleFormStreamEvent(ev) {
  console.log("[PA DEBUG][FORM STREAM]", ev.event, ev);
  if (ev.event === "session") {
    // server-side sends null; the real session_tab_id came from the
    // background's synchronous ASK response — nothing to do here.
    return;
  }
  if (ev.event === "meta") {
    answerEl.textContent = `Detected ${ev.total_fields} field(s) — filling direct fields immediately, semantic answers to follow.`;
    return;
  }
  if (ev.event === "phase") {
    if (ev.phase === "direct_done" && Array.isArray(ev.fields)) {
      // Render all rows now — direct ones are final, semantic ones are placeholders.
      showForm(ev.fields);
      const directCount = ev.fields.filter((f) => f && f.state === "ready" && f.route && f.route.startsWith("direct")).length;
      statusEl.textContent = `Direct fields ready (${directCount}). Semantic fields in progress…`;
      return;
    }
    if (ev.phase === "generating" && Array.isArray(ev.field_ids)) {
      for (const fid of ev.field_ids) {
        const row = _rowByFieldId(fid);
        if (row) _setBadge(row, "generating");
        const control = row?.querySelector("input, textarea");
        if (control) control.placeholder = "Generating answer…";
      }
      return;
    }
  }
  if (ev.event === "field") {
    patchRowFromField(ev);
    return;
  }
  if (ev.event === "done") {
    const c = ev.counts || {};
    statusEl.textContent =
      `Form fill complete · ready=${c.ready ?? "?"} unknown=${c.unknown ?? "?"} · ${ev.total_ms ?? "?"} ms total`;
    return;
  }
  if (ev.event === "error") {
    answerEl.textContent = `Error: ${ev.error}`;
    statusEl.textContent = "";
    return;
  }
}

function autoFillReadyRows() {
  const rows = formFields.querySelectorAll('.field-row[data-state="ready"]');
  for (const row of rows) {
    // Skip if the user already Skipped or manually filled this row.
    if (row.dataset.autofilled === "1") continue;
    const control = row.querySelector("input, textarea");
    const fillBtn = row.querySelector(".fill-btn");
    const rowStatus = row.querySelector(".status");
    if (!control?.value || !fillBtn || fillBtn.disabled) continue;
    row.dataset.autofilled = "1";
    fillBtn.disabled = true;
    (async () => {
      try {
        const resp = await sendFillFromRow(row, control, control.value);
        if (!resp?.ok) throw new Error(resp?.error ?? "Auto-fill failed.");
        rowStatus.className = "status ok";
        rowStatus.textContent = `Auto-filled: ${(resp.filled ?? control.value).slice(0, 80)}`;
      } catch (e) {
        rowStatus.className = "status err";
        rowStatus.textContent = `Auto-fill: ${e.message ?? e}`;
      } finally {
        // Re-enable so the user can edit and click Fill to overwrite.
        fillBtn.disabled = false;
      }
    })();
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

function hideDraft() {
  draftArea.style.display = "none";
  draftBody.value = "";
  draftError.style.display = "none";
  draftError.textContent = "";
}

function showDraft(body) {
  draftBody.value = body ?? "";
  draftArea.style.display = "block";
  draftError.style.display = "none";
  draftError.textContent = "";
  draftBody.focus();
}

askBtn.addEventListener("click", async () => {
  const question = q.value.trim();
  if (!question) return;
  console.log("[PA DEBUG][SIDEPANEL ASK CLICK]");
  askBtn.disabled = true;
  answerEl.textContent = "";
  statusEl.textContent = "Reading page and asking model...";
  hideDraft();
  hideForm();
  try {
    console.log("[PA DEBUG][SIDEPANEL SEND ASK]");
    const resp = await chrome.runtime.sendMessage({ type: "ASK", question });
    console.log("[PA DEBUG][SIDEPANEL ASK RESPONSE]", { ok: resp?.ok, streaming: resp?.streaming, error: resp?.error });
    if (!resp?.ok) throw new Error(resp?.error ?? "Unknown error.");
    // Pin the form session to the tab that produced these fields.
    if (typeof resp.session_tab_id === "number") {
      formSessionTabId = resp.session_tab_id;
      console.log("[FORM DEBUG][SESSION]",
        JSON.stringify({ detectedTabId: resp.session_tab_id, storedTabId: formSessionTabId }));
    }
    const tools = (resp.trace ?? []).map((t) => t.tool).filter(Boolean).join(" -> ");
    statusEl.textContent = [
      resp.title ? `Source: ${resp.title}` : "",
      tools ? `Tools: ${tools}` : "Tools: (none)",
    ].filter(Boolean).join(" · ");

    if (resp.streaming) {
      // Progressive form-fill: background is streaming NDJSON via
      // FORM_STREAM_EVENT messages. The event handler drives rendering.
      answerEl.textContent = "Detecting fields…";
      return;
    }

    const draft = resp.draft;
    if (draft?.type === "form_fill" && Array.isArray(draft.fields)) {
      answerEl.textContent =
        `Detected ${draft.fields.length} form field(s). Review each below before anything is written.`;
      showForm(draft.fields);
    } else if (resp.requires_confirmation && (draft?.type === "email" || draft?.body)) {
      answerEl.textContent =
        "Drafted a reply. Review below before inserting into Gmail.";
      showDraft(draft.body);
    } else {
      answerEl.textContent = resp.answer ?? "(empty response)";
    }
  } catch (e) {
    console.warn("[PA DEBUG][SIDEPANEL ASK ERROR]", e?.message ?? e);
    statusEl.textContent = "";
    answerEl.textContent = `Error: ${e.message ?? e}`;
  } finally {
    askBtn.disabled = false;
  }
});

insertBtn.addEventListener("click", async () => {
  insertBtn.disabled = true;
  draftError.style.display = "none";
  try {
    const resp = await chrome.runtime.sendMessage({
      type: "INSERT_DRAFT",
      text: draftBody.value,
    });
    if (!resp?.ok) throw new Error(resp?.error ?? "Insert failed.");
    // Draft is now in Gmail's compose. Keep the draft visible in case the
    // user wants to edit and re-insert (execCommand appends at the caret).
    statusEl.textContent = "Inserted into Gmail compose. Review and send in Gmail.";
  } catch (e) {
    draftError.textContent = String(e.message ?? e);
    draftError.style.display = "block";
  } finally {
    insertBtn.disabled = false;
  }
});

rejectBtn.addEventListener("click", () => {
  hideDraft();
  statusEl.textContent = "Draft rejected.";
});

approveAllBtn.addEventListener("click", () => {
  // "Fill all ready" = only READY rows (see classifyState). GENERATE,
  // REVIEW, UNKNOWN rows all need an explicit per-row choice from the user.
  const rows = formFields.querySelectorAll('.field-row[data-state="ready"]');
  for (const row of rows) {
    const input = row.querySelector("input[type=text]");
    const fillBtn = row.querySelector(".fill-btn");
    if (input?.value && fillBtn && !fillBtn.disabled) {
      fillBtn.click();
    }
  }
});

closeFormBtn.addEventListener("click", () => {
  hideForm();
  statusEl.textContent = "Form preview closed.";
});

// ---- Cmd/Ctrl+Shift+F entry points ----
// Background sets a storage flag AND broadcasts RUN_FILL. Either can
// arrive first depending on whether the panel was already open. The
// askBtn.disabled guard inside the click handler dedupes if both fire
// close together, so pressing the shortcut still runs the pipeline
// exactly once.
function runFillFromShortcut() {
  if (askBtn.disabled) return;
  q.value = "fill this form";
  askBtn.click();
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "RUN_FILL") { runFillFromShortcut(); return; }
  if (msg?.type === "FORM_STREAM_EVENT" && msg.event) {
    try { handleFormStreamEvent(msg.event); } catch (e) { console.warn("[PA DEBUG] stream handler err", e); }
    return;
  }
});

chrome.storage.local.get("pending_fill").then((r) => {
  const ts = r?.pending_fill;
  if (ts && Date.now() - ts < 5000) {
    chrome.storage.local.remove("pending_fill");
    runFillFromShortcut();
  }
}).catch(() => {});
