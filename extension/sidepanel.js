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

function hideForm() {
  formArea.style.display = "none";
  formFields.innerHTML = "";
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

function showForm(fields) {
  formFields.innerHTML = "";
  for (const [i, f] of fields.entries()) {
    const row = document.createElement("div");
    row.className = "field-row";
    const state = f.state === "ready" ? "ready" : "unknown";
    row.dataset.state = state;
    const label = f.label || f.selector || `field ${i + 1}`;
    const selectorAttr = escapeAttr(f.selector || "");
    const badge = state === "ready"
      ? `<span class="state-ready">READY</span>`
      : `<span class="state-unknown">UNKNOWN</span>`;
    const value = f.value ?? "";
    const placeholder = state === "unknown" ? "Enter a value" : "";
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
    // Frame identity: detection ran in a specific frame and returned
    // f.frameId. Fill MUST be routed back to the same frame or the
    // selector won't match. Store on the row so both handlers see it.
    if (typeof f.frameId === "number") row.dataset.frameId = String(f.frameId);
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
        const frameId = row.dataset.frameId ? Number(row.dataset.frameId) : undefined;
        const resp = await chrome.runtime.sendMessage({
          type: "FILL_FIELD",
          selector: control_el.dataset.selector,
          value: v,
          frameId,
        });
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
  // Auto-fill READY rows. Each row's `data-state="ready"` is set by the
  // server-side pipeline only after option-fit + phone-format + OBVIOUS
  // deterministic-or-Qwen-answer validation. We don't re-validate here —
  // we route the same value through the existing FILL_FIELD path used by
  // the manual Fill button. No new Ollama calls, no form submission.
  autoFillReadyRows();
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
        const frameId = row.dataset.frameId ? Number(row.dataset.frameId) : undefined;
        const resp = await chrome.runtime.sendMessage({
          type: "FILL_FIELD",
          selector: control.dataset.selector,
          value: control.value,
          frameId,
        });
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
  askBtn.disabled = true;
  answerEl.textContent = "";
  statusEl.textContent = "Reading page and asking model...";
  hideDraft();
  hideForm();
  try {
    const resp = await chrome.runtime.sendMessage({ type: "ASK", question });
    if (!resp?.ok) throw new Error(resp?.error ?? "Unknown error.");
    const tools = (resp.trace ?? []).map((t) => t.tool).filter(Boolean).join(" -> ");
    statusEl.textContent = [
      resp.title ? `Source: ${resp.title}` : "",
      tools ? `Tools: ${tools}` : "Tools: (none)",
    ].filter(Boolean).join(" · ");

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
  if (msg?.type === "RUN_FILL") runFillFromShortcut();
});

chrome.storage.local.get("pending_fill").then((r) => {
  const ts = r?.pending_fill;
  if (ts && Date.now() - ts < 5000) {
    chrome.storage.local.remove("pending_fill");
    runFillFromShortcut();
  }
}).catch(() => {});
