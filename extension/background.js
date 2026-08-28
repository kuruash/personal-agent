// Background service worker: side panel ASK -> content script context bundle
// -> POST /ask (server runs the tool-calling loop) -> answer back to panel.
// Also relays INSERT_DRAFT (panel -> active tab's gmail.js) — the only path
// through which a draft ever touches the DOM. Nothing else auto-inserts.

const SERVER_URL = "http://127.0.0.1:8000/ask";

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.warn("setPanelBehavior failed:", err));

// Cmd/Ctrl+Shift+F: trigger the exact same flow as typing "fill this form"
// and clicking Ask in the side panel. We do NOT reimplement any of the
// detect/backend/render pipeline here — we only open the panel and hand
// off to it. The panel drives the ASK message like a user would.
//
// Handoff has two carriers so it works whether the panel is already open
// or needs to be opened by this action:
//   - chrome.storage.local flag with a timestamp, checked by the panel on
//     load (covers the "panel wasn't open" case).
//   - runtime broadcast (covers the "panel is already open" case).
// The panel dedupes via the askBtn.disabled guard, so double-delivery is
// safe: the first fires the pipeline, the second is a no-op.
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "fill-current-form") return;
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!tab?.id) return;
    await chrome.storage.local.set({ pending_fill: Date.now() });
    try { await chrome.sidePanel.open({ tabId: tab.id }); } catch (_) { /* panel may already be open */ }
    // Broadcast in case the panel was already loaded (its storage on-load
    // handler already ran before the flag was written).
    chrome.runtime.sendMessage({ type: "RUN_FILL" }).catch(() => {});
  } catch (err) {
    console.warn("fill-current-form command failed:", err);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "ASK") {
    handleAsk(msg.question)
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: String(err?.message ?? err) }));
    return true;
  }
  if (msg?.type === "INSERT_DRAFT") {
    relayInsertDraft(msg.text ?? "")
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: String(err?.message ?? err) }));
    return true;
  }
  if (msg?.type === "FILL_FIELD") {
    relayFillField(msg.selector, msg.value ?? "", msg.frameId)
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: String(err?.message ?? err) }));
    return true;
  }
});

async function handleAsk(question) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) throw new Error("No active tab.");

  // Top-frame context (url, title, page_text, gmail thread, youtube). We
  // pin frameId: 0 because content.js now guards GET_PAGE_CONTEXT to only
  // respond in the top frame.
  const ctx = await sendToTab(tab.id, { type: "GET_PAGE_CONTEXT" }, { frameId: 0 });
  if (!ctx?.ok) throw new Error(ctx?.error ?? "Failed to read page context.");

  // Aggregate form_fields from EVERY frame in the tab. SmartRecruiters
  // (and most ATS embeds) mount the application form in a cross-origin
  // child iframe; top-frame-only detection returns nothing. Each field
  // is annotated with the frameId that produced it so FILL_FIELD can be
  // routed back to the same frame later.
  const formFields = await collectFormFieldsAllFrames(tab.id);
  try {
    console.log("[FORM DEBUG] sending context:", {
      url: ctx.url,
      form_field_count: formFields.length,
      per_frame_counts: formFields.reduce((acc, f) => {
        acc[f.frameId] = (acc[f.frameId] || 0) + 1; return acc;
      }, {}),
      form_fields: formFields,
    });
  } catch (_) {}

  const context = {
    url: ctx.url,
    title: ctx.title,
    page_text: ctx.page_text ?? "",
    is_youtube: !!ctx.is_youtube,
    video_id: ctx.video_id ?? null,
    transcript: ctx.transcript ?? null,
    email_thread: ctx.email_thread ?? null,
    form_fields: formFields.length > 0 ? formFields : null,
  };

  const res = await fetch(SERVER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, context }),
  });
  if (!res.ok) throw new Error(`Server ${res.status}: ${await res.text()}`);
  const data = await res.json();
  return {
    ok: true,
    answer: data.answer,
    trace: data.trace,
    title: ctx.title,
    requires_confirmation: !!data.requires_confirmation,
    draft: data.draft ?? null,
  };
}

async function relayInsertDraft(text) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) return { ok: false, error: "No active tab." };
  return await sendToTab(tab.id, { type: "INSERT_DRAFT", text });
}

async function relayFillField(selector, value, frameId) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) return { ok: false, error: "No active tab." };
  // Pin the frame the field came from — detection and fill MUST agree on
  // frame identity, otherwise the selector will miss.
  const opts = typeof frameId === "number" ? { frameId } : {};
  return await sendToTab(tab.id, { type: "FILL_FIELD", selector, value }, opts);
}

async function collectFormFieldsAllFrames(tabId) {
  // Runs detectFormFields() in every frame of the tab (content scripts
  // are declared with all_frames: true). Returns a flat array of fields,
  // each annotated with the producing frameId.
  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        // eslint-disable-next-line no-undef
        return typeof detectFormFields === "function" ? detectFormFields() : [];
      },
    });
  } catch (e) {
    console.warn("[FORM DEBUG] executeScript(allFrames) failed:", e);
    return [];
  }
  const out = [];
  for (const r of results || []) {
    const frameId = r.frameId;
    const fields = Array.isArray(r.result) ? r.result : [];
    for (const f of fields) {
      f.frameId = frameId;
      out.push(f);
    }
  }
  return out;
}

function sendToTab(tabId, message, options) {
  return new Promise((resolve, reject) => {
    const cb = (response) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message));
      else resolve(response);
    };
    if (options && Object.keys(options).length > 0) {
      chrome.tabs.sendMessage(tabId, message, options, cb);
    } else {
      chrome.tabs.sendMessage(tabId, message, cb);
    }
  });
}
