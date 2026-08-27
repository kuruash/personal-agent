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

function showForm(fields) {
  formFields.innerHTML = "";
  for (const [i, f] of fields.entries()) {
    const row = document.createElement("div");
    row.className = "field-row";
    const conf = f.confidence || "none";
    const label = f.label || f.selector || `field ${i + 1}`;
    const source = f.source || "";
    const selectorAttr = escapeAttr(f.selector || "");

    if (conf === "ambiguous" && Array.isArray(f.candidates) && f.candidates.length >= 2) {
      // Render a radio picker over the candidates instead of a free-text
      // input — resolving ambiguity should be a pick, not typing.
      const radios = f.candidates.map((c, ci) => {
        const cid = `amb-${i}-${ci}`;
        const cval = c.value ?? "";
        const cdisplay = cval === "" ? "(unset)" : cval;
        return `
          <label class="cand" for="${cid}">
            <input type="radio" id="${cid}" name="amb-${i}" value="${escapeAttr(cval)}" ${ci === 0 ? "checked" : ""} />
            <b>${escapeHtml(c.profile_key || "")}</b>
            <span class="cand-sim">(sim ${c.similarity ?? "?"})</span>
            — <span class="cand-desc">${escapeHtml(c.description || "")}</span><br />
            <span class="cand-val">→ ${escapeHtml(cdisplay)}</span>
          </label>
        `;
      }).join("");
      row.innerHTML = `
        <div class="lbl">${escapeHtml(label)}</div>
        <div class="meta">
          <span class="conf-ambiguous">AMBIGUOUS</span>
          · ${escapeHtml(source)}
          · <code>${escapeHtml(f.selector || "")}</code>
        </div>
        <div class="candidates" data-selector="${selectorAttr}">
          ${radios}
        </div>
        <div class="actions">
          <button class="fill-btn">Fill selected</button>
          <button class="skip-btn">Skip</button>
        </div>
        <div class="status"></div>
      `;
      const fillBtn = row.querySelector(".fill-btn");
      const skipBtn = row.querySelector(".skip-btn");
      const statusEl = row.querySelector(".status");
      fillBtn.addEventListener("click", async () => {
        const chosen = row.querySelector(`input[name="amb-${i}"]:checked`);
        const v = chosen?.value ?? "";
        if (!v) {
          statusEl.className = "status err";
          statusEl.textContent = "Selected candidate has no stored value.";
          return;
        }
        fillBtn.disabled = true;
        try {
          const resp = await chrome.runtime.sendMessage({
            type: "FILL_FIELD",
            selector: f.selector,
            value: v,
          });
          if (!resp?.ok) throw new Error(resp?.error ?? "Fill failed.");
          statusEl.className = "status ok";
          statusEl.textContent = `Filled: ${resp.filled ?? v}`;
        } catch (e) {
          statusEl.className = "status err";
          statusEl.textContent = String(e.message ?? e);
        } finally {
          fillBtn.disabled = false;
        }
      });
      skipBtn.addEventListener("click", () => {
        row.style.opacity = "0.5";
        fillBtn.disabled = true;
        skipBtn.disabled = true;
        statusEl.className = "status";
        statusEl.textContent = "Skipped.";
      });
      formFields.appendChild(row);
      continue;
    }

    // Non-ambiguous path (high / medium / low / none): editable text input.
    const value = f.value ?? "";
    const needsInput = conf === "none" || conf === "low" || value == null || value === "";
    const placeholder = needsInput
      ? "I don't have this — enter a value"
      : "";
    row.innerHTML = `
      <div class="lbl">${escapeHtml(label)}</div>
      <div class="meta">
        <span class="conf-${conf}">${conf.toUpperCase()}</span>
        · ${escapeHtml(source)}
        · <code>${escapeHtml(f.selector || "")}</code>
      </div>
      <input type="text" data-selector="${selectorAttr}"
             value="${escapeAttr(value ?? "")}"
             placeholder="${escapeAttr(placeholder)}" />
      <div class="actions">
        <button class="fill-btn">Fill</button>
        <button class="skip-btn">Skip</button>
      </div>
      <div class="status"></div>
    `;
    const input = row.querySelector("input");
    const fillBtn = row.querySelector(".fill-btn");
    const skipBtn = row.querySelector(".skip-btn");
    const statusEl = row.querySelector(".status");
    fillBtn.addEventListener("click", async () => {
      const v = input.value;
      if (!v) {
        statusEl.className = "status err";
        statusEl.textContent = "Value is empty — nothing to fill.";
        return;
      }
      fillBtn.disabled = true;
      try {
        const resp = await chrome.runtime.sendMessage({
          type: "FILL_FIELD",
          selector: input.dataset.selector,
          value: v,
        });
        if (!resp?.ok) throw new Error(resp?.error ?? "Fill failed.");
        statusEl.className = "status ok";
        statusEl.textContent = `Filled: ${resp.filled ?? v}`;
      } catch (e) {
        statusEl.className = "status err";
        statusEl.textContent = String(e.message ?? e);
      } finally {
        fillBtn.disabled = false;
      }
    });
    skipBtn.addEventListener("click", () => {
      row.style.opacity = "0.5";
      fillBtn.disabled = true;
      skipBtn.disabled = true;
      statusEl.className = "status";
      statusEl.textContent = "Skipped.";
    });
    formFields.appendChild(row);
  }
  formArea.style.display = "block";
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
  // "High-confidence" here = the row's meta contains "HIGH" and the input
  // has a non-empty value. We just simulate clicks on those rows' Fill
  // buttons — actual writes still go through the same per-field code path,
  // so each still shows individual success/error status.
  const rows = formFields.querySelectorAll(".field-row");
  for (const row of rows) {
    const conf = row.querySelector(".meta .conf-high");
    const input = row.querySelector("input");
    const fillBtn = row.querySelector(".fill-btn");
    if (conf && input?.value && !fillBtn.disabled) {
      fillBtn.click();
    }
  }
});

closeFormBtn.addEventListener("click", () => {
  hideForm();
  statusEl.textContent = "Form preview closed.";
});
