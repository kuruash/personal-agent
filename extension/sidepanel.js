const q = document.getElementById("q");
const askBtn = document.getElementById("ask");
const answerEl = document.getElementById("answer");
const statusEl = document.getElementById("status");

askBtn.addEventListener("click", async () => {
  const question = q.value.trim();
  if (!question) return;
  askBtn.disabled = true;
  answerEl.textContent = "";
  statusEl.textContent = "Reading page and asking model...";
  try {
    const resp = await chrome.runtime.sendMessage({ type: "ASK", question });
    if (!resp?.ok) throw new Error(resp?.error ?? "Unknown error.");
    statusEl.textContent = resp.title ? `Source: ${resp.title}` : "";
    answerEl.textContent = resp.answer ?? "(empty response)";
  } catch (e) {
    statusEl.textContent = "";
    answerEl.textContent = `Error: ${e.message ?? e}`;
  } finally {
    askBtn.disabled = false;
  }
});
