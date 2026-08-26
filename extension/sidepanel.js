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
    const tools = (resp.trace ?? []).map((t) => t.tool).join(" -> ");
    statusEl.textContent = [
      resp.title ? `Source: ${resp.title}` : "",
      tools ? `Tools: ${tools}` : "Tools: (none)",
    ].filter(Boolean).join(" · ");
    answerEl.textContent = resp.answer ?? "(empty response)";
  } catch (e) {
    statusEl.textContent = "";
    answerEl.textContent = `Error: ${e.message ?? e}`;
  } finally {
    askBtn.disabled = false;
  }
});
