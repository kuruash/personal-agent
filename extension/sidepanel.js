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
let formFillStats = null;
let requestInFlight = false;

function setRequestInFlight(active) {
  requestInFlight = active;
  askBtn.disabled = active;
  document.body.classList.toggle("is-loading", active);
}

function resetFormFillStats() {
  formFillStats = {
    autoTotal: 0,
    autoFilled: 0,
    autoFailed: 0,
    reviewTotal: 0,
    streamDone: false,
  };
}

function isAutoFillCandidate(f) {
  return f?.state === "ready"
    && f?.requires_review === false
    && f?.confidence === "high"
    && !!String(f?.value ?? "").trim();
}

function shouldShowForReview(f) {
  return !isAutoFillCandidate(f);
}

function reviewSummaryText() {
  if (!formFillStats) return "";
  const filled = formFillStats.autoFilled;
  const failed = formFillStats.autoFailed;
  const review = formFillStats.reviewTotal + failed;
  const pending = Math.max(0, formFillStats.autoTotal - filled - failed);
  if (pending > 0) {
    return `Filling ${formFillStats.autoTotal} confident field(s) automatically. ${review} field(s) need review.`;
  }
  if (failed > 0) {
    return `Filled ${filled} field(s) automatically. ${review} field(s) need review.`;
  }
  if (review === 0) {
    return `Filled ${filled} field(s) automatically. No review needed.`;
  }
  return `Filled ${filled} field(s) automatically. ${review} field(s) need review.`;
}

function updateReviewSummary() {
  const text = reviewSummaryText();
  if (text) setAnswerMessage(text);
}

function autoFillPendingCount() {
  if (!formFillStats) return 0;
  return Math.max(0, formFillStats.autoTotal - formFillStats.autoFilled - formFillStats.autoFailed);
}

function finishFormRequestIfSettled() {
  if (formFillStats?.streamDone && autoFillPendingCount() === 0) {
    setRequestInFlight(false);
  }
}

function setAnswerMessage(text, kind = "assistant") {
  const safeText = text ?? "";
  const label = kind === "error" ? "Personal Agent" : "Personal Agent";
  answerEl.className = kind === "error" ? "message-list has-error" : "message-list";
  if (kind === "loading") {
    answerEl.innerHTML = `
      <div class="loading-message">
        <span class="loading-dot" aria-hidden="true"></span>
        <span>${escapeHtml(safeText)}</span>
      </div>
    `;
    return;
  }
  if (kind === "error") {
    answerEl.innerHTML = `
      <div class="error-message">
        <div class="error-title">${escapeHtml(safeText)}</div>
        <div class="error-detail">Try refreshing the page and opening Personal Agent again.</div>
      </div>
    `;
    return;
  }
  answerEl.innerHTML = `
    <div class="assistant-message">
      <div class="assistant-label">${escapeHtml(label)}</div>
      <div>${escapeHtml(safeText)}</div>
    </div>
  `;
}

function userFacingError(error) {
  const msg = String(error?.message ?? error ?? "");
  if (/Could not establish connection|Receiving end does not exist|could not access this page/i.test(msg)) {
    return "Couldn't connect to this page.";
  }
  if (/No active tab/i.test(msg)) return "No active page found.";
  if (/Failed to fetch|NetworkError|fetch/i.test(msg)) return "Couldn't reach the local Personal Agent server.";
  return "Something went wrong.";
}

function resizeComposer() {
  q.style.height = "auto";
  q.style.height = `${Math.min(q.scrollHeight, 128)}px`;
}

function hideForm() {
  formArea.style.display = "none";
  formFields.innerHTML = "";
}

// Single fill routing path used by row Fill and the explicit "Fill all ready" action.
async function sendFillPayload({ frameId, selector, value, shadowPath }) {
  return await chrome.runtime.sendMessage({
    type: "FILL_FIELD",
    tabId: formSessionTabId,
    frameId,
    selector,
    value,
    shadowPath,
  });
}

async function sendFillFromField(f, value) {
  if (typeof formSessionTabId !== "number") {
    return { ok: false, error: "Form session tabId missing — press Cmd+Shift+F again." };
  }
  return await sendFillPayload({
    frameId: typeof f.frameId === "number" ? f.frameId : undefined,
    selector: f.selector,
    value,
    shadowPath: Array.isArray(f.shadowPath) ? f.shadowPath : [],
  });
}

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
  return await sendFillPayload({
    frameId,
    selector: controlEl.dataset.selector,
    value,
    shadowPath,
  });
}

// Choose textarea vs single-line based on answer length or the underlying field type.
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
        <div><b>confidence:</b> ${escapeHtml(f.confidence || "low")}</div>
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

function metaHtml(state, confidence) {
  const badge = STATE_LABEL[state] || STATE_LABEL.unknown;
  const conf = confidence ? `<span class="confidence-label">${escapeHtml(confidence)}</span>` : "";
  return `${badge}${conf}`;
}

function _placeholder_for(state) {
  if (state === "unknown") return "Enter a value";
  if (state === "retrieving") return "Retrieving evidence…";
  if (state === "generating") return "Generating answer…";
  return "";
}

function createFieldRow(f, fallbackIndex = 0) {
    const row = document.createElement("div");
    row.className = "field-row";
    // "ready" | "unknown" | "retrieving" | "generating"
    const state = f.state && STATE_LABEL[f.state] ? f.state : "unknown";
    row.dataset.state = state;
    if (f.confidence) row.dataset.confidence = f.confidence;
    if (typeof f.field_id === "number") row.dataset.fieldId = String(f.field_id);
    const label = f.label || f.selector || `field ${fallbackIndex + 1}`;
    const selectorAttr = escapeAttr(f.selector || "");
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
      <div class="meta">${metaHtml(state, f.confidence)}</div>
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
      row.dataset.state = "skipped";
      fillBtn.disabled = true;
      skipBtn.disabled = true;
      rowStatus.className = "status";
      rowStatus.textContent = "Skipped.";
    });
    return row;
}

function showForm(fields) {
  formFields.innerHTML = "";
  for (const [i, f] of fields.entries()) {
    const row = createFieldRow(f, i);
    formFields.appendChild(row);
  }
  formArea.style.display = fields.length > 0 ? "block" : "none";
}

async function autoFillConfidentField(f, alreadyCounted = false) {
  if (!formFillStats || f.__autoFillAttempted) return;
  f.__autoFillAttempted = true;
  if (!alreadyCounted) formFillStats.autoTotal += 1;
  try {
    const resp = await sendFillFromField(f, f.value);
    if (!resp?.ok) throw new Error(resp?.error ?? "Auto-fill failed.");
    formFillStats.autoFilled += 1;
    f.status = "filled";
  } catch (e) {
    formFillStats.autoFailed += 1;
    f.status = "failed";
    f.requires_review = true;
    f.confidence = "low";
    console.warn("[SidePanel] Confident auto-fill failed; moved to review:", e?.message ?? e);
    showOrPatchReviewField(f);
  } finally {
    updateReviewSummary();
    finishFormRequestIfSettled();
  }
}

function showOrPatchReviewField(f) {
  const existing = _rowByFieldId(f.field_id);
  if (existing) {
    patchRowFromField(f);
    return;
  }
  formFields.appendChild(createFieldRow(f, formFields.children.length));
  formArea.style.display = "block";
}


// ── Streaming patches ───────────────────────────────────────

function _rowByFieldId(fid) {
  return formFields.querySelector(`.field-row[data-field-id="${fid}"]`);
}

function _setBadge(row, state) {
  row.dataset.state = state;
  const badgeEl = row.querySelector(".meta");
  if (badgeEl) badgeEl.innerHTML = metaHtml(state, row.dataset.confidence);
}

function patchRowFromField(f) {
  // f: full plan entry from server (may have state ready/unknown/retrieving/generating)
  const row = _rowByFieldId(f.field_id);
  if (!row) return;
  const state = f.state && STATE_LABEL[f.state] ? f.state : "unknown";
  if (f.confidence) row.dataset.confidence = f.confidence;
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
  if (state === "ready") row.dataset.autofilled = "";
}

function handleFormStreamEvent(ev) {
  console.log("[PA DEBUG][FORM STREAM]", ev.event, ev);
  if (ev.event === "session") {
    // server-side sends null; the real session_tab_id came from the
    // background's synchronous ASK response — nothing to do here.
    return;
  }
  if (ev.event === "meta") {
    resetFormFillStats();
    setAnswerMessage(`Detected ${ev.total_fields} field(s). Resolving answers...`, "loading");
    return;
  }
  if (ev.event === "phase") {
    if (ev.phase === "direct_done" && Array.isArray(ev.fields)) {
      if (!formFillStats) resetFormFillStats();
      const reviewFields = ev.fields.filter(shouldShowForReview);
      const autoFields = ev.fields.filter(isAutoFillCandidate);
      formFillStats.autoTotal += autoFields.length;
      formFillStats.reviewTotal = reviewFields.length;
      if (reviewFields.length > 0) showForm(reviewFields);
      else hideForm();
      for (const f of autoFields) {
        autoFillConfidentField(f, true);
      }
      updateReviewSummary();
      statusEl.textContent = reviewFields.length > 0
        ? "Review the remaining fields below while analysis finishes."
        : "No review needed so far. Finishing analysis...";
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
    if (isAutoFillCandidate(ev)) {
      autoFillConfidentField(ev);
    } else {
      if (!formFillStats) resetFormFillStats();
      if (!_rowByFieldId(ev.field_id)) {
        formFillStats.reviewTotal += 1;
        showOrPatchReviewField(ev);
      } else {
        patchRowFromField(ev);
      }
      updateReviewSummary();
    }
    return;
  }
  if (ev.event === "done") {
    const c = ev.counts || {};
    if (!formFillStats) resetFormFillStats();
    formFillStats.streamDone = true;
    const pending = autoFillPendingCount();
    statusEl.textContent = pending > 0
      ? `Form analysis complete · ${pending} automatic fill(s) finishing.`
      : `Form fill complete · ready=${c.ready ?? "?"} unknown=${c.unknown ?? "?"} · ${ev.total_ms ?? "?"} ms total. You can keep asking questions.`;
    updateReviewSummary();
    finishFormRequestIfSettled();
    return;
  }
  if (ev.event === "error") {
    console.warn("[SidePanel] Form stream error:", ev.error);
    setAnswerMessage("Couldn't finish form analysis.", "error");
    statusEl.textContent = "";
    if (formFillStats) formFillStats.streamDone = true;
    setRequestInFlight(false);
    return;
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
  if (!question || requestInFlight) return;
  console.log("[PA DEBUG][SIDEPANEL ASK CLICK]");
  setRequestInFlight(true);
  setAnswerMessage("Thinking...", "loading");
  statusEl.textContent = "Reading page...";
  hideDraft();
  hideForm();
  let isStreaming = false;
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
      isStreaming = true;
      // Progressive form-fill: background is streaming NDJSON via
      // FORM_STREAM_EVENT messages. The event handler drives rendering.
      setAnswerMessage("Detecting fields...", "loading");
      return;
    }

    const draft = resp.draft;
    if (draft?.type === "form_fill" && Array.isArray(draft.fields)) {
      setAnswerMessage(`Detected ${draft.fields.length} form field(s). Review each below before anything is written.`);
      showForm(draft.fields);
    } else if (resp.requires_confirmation && (draft?.type === "email" || draft?.body)) {
      setAnswerMessage("Drafted a reply. Review below before inserting into Gmail.");
      showDraft(draft.body);
    } else {
      setAnswerMessage(resp.answer ?? "(empty response)");
    }
  } catch (e) {
    console.warn("[SidePanel] Ask failed:", e);
    statusEl.textContent = "";
    setAnswerMessage(userFacingError(e), "error");
  } finally {
    if (!isStreaming) setRequestInFlight(false);
  }
});

q.addEventListener("input", resizeComposer);
q.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    if (!askBtn.disabled) askBtn.click();
  }
});
resizeComposer();

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
  // "Fill reviewed" = explicit user action on READY review rows only.
  // UNKNOWN rows still need a user-entered value before a row Fill can write.
  const rows = formFields.querySelectorAll('.field-row[data-state="ready"]');
  for (const row of rows) {
    const input = row.querySelector("input, textarea");
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
  if (requestInFlight) return;
  q.value = "fill this form";
  resizeComposer();
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
