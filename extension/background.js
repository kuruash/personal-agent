// Background service worker: side panel ASK -> content script context bundle
// -> POST /ask (server runs the tool-calling loop) -> answer back to panel.

const SERVER_URL = "http://127.0.0.1:8000/ask";

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.warn("setPanelBehavior failed:", err));

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "ASK") return;
  handleAsk(msg.question).then(sendResponse).catch((err) => {
    sendResponse({ ok: false, error: String(err?.message ?? err) });
  });
  return true;
});

async function handleAsk(question) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) throw new Error("No active tab.");

  const ctx = await sendToTab(tab.id, { type: "GET_PAGE_CONTEXT" });
  if (!ctx?.ok) throw new Error(ctx?.error ?? "Failed to read page context.");

  const context = {
    url: ctx.url,
    title: ctx.title,
    page_text: ctx.page_text ?? "",
    is_youtube: !!ctx.is_youtube,
    video_id: ctx.video_id ?? null,
    transcript: ctx.transcript ?? null,
  };

  const res = await fetch(SERVER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, context }),
  });
  if (!res.ok) throw new Error(`Server ${res.status}: ${await res.text()}`);
  const data = await res.json();
  return { ok: true, answer: data.answer, trace: data.trace, title: ctx.title };
}

function sendToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (response) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message));
      else resolve(response);
    });
  });
}
