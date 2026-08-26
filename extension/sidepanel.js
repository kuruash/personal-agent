const q = document.getElementById("q");
const askBtn = document.getElementById("ask");
const answerEl = document.getElementById("answer");
const statusEl = document.getElementById("status");

const draftArea = document.getElementById("draft-area");
const draftBody = document.getElementById("draft-body");
const draftError = document.getElementById("draft-error");
const insertBtn = document.getElementById("insert-btn");
const rejectBtn = document.getElementById("reject-btn");

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
  try {
    const resp = await chrome.runtime.sendMessage({ type: "ASK", question });
    if (!resp?.ok) throw new Error(resp?.error ?? "Unknown error.");
    const tools = (resp.trace ?? []).map((t) => t.tool).filter(Boolean).join(" -> ");
    statusEl.textContent = [
      resp.title ? `Source: ${resp.title}` : "",
      tools ? `Tools: ${tools}` : "Tools: (none)",
    ].filter(Boolean).join(" · ");

    if (resp.requires_confirmation && resp.draft?.body) {
      answerEl.textContent =
        "Drafted a reply. Review below before inserting into Gmail.";
      showDraft(resp.draft.body);
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
