// Background service worker: relays the side panel's "ASK" request to the
// active tab's content script, forwards the page text + question to the local
// FastAPI /ask endpoint, and returns the answer to the panel.

const SERVER_URL = "http://127.0.0.1:8000/ask";

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.warn("setPanelBehavior failed:", err));

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "ASK") return;
  handleAsk(msg.question).then(sendResponse).catch((err) => {
    sendResponse({ ok: false, error: String(err?.message ?? err) });
  });
  return true; // keep the message channel open for the async response
});

async function handleAsk(question) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) throw new Error("No active tab.");

  const pageResp = await sendToTab(tab.id, { type: "GET_PAGE_TEXT" });
  if (!pageResp?.ok) throw new Error(pageResp?.error ?? "Failed to read page.");

  const res = await fetch(SERVER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page_text: pageResp.text ?? "", question }),
  });
  if (!res.ok) throw new Error(`Server ${res.status}: ${await res.text()}`);
  const data = await res.json();
  return { ok: true, answer: data.answer, url: pageResp.url, title: pageResp.title };
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
