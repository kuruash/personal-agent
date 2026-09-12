// Background service worker: side panel ASK -> content script context bundle
// -> POST /ask (server runs the tool-calling loop) -> answer back to panel.
// Also relays INSERT_DRAFT (panel -> active tab's gmail.js) — the only path
// through which a draft ever touches the DOM. Nothing else auto-inserts.

const SERVER_URL = "http://127.0.0.1:8000/ask";
const SERVER_URL_FORM_STREAM = "http://127.0.0.1:8000/ask/form_stream";
const FETCH_TIMEOUT_MS = 90_000;
const CONTENT_SCRIPT_FILES = ["gmail.js", "formdetect.js", "content.js"];
const RECEIVING_END_ERROR = "Could not establish connection. Receiving end does not exist.";

// Same shape as the server-side fast-intent regex. If the question and
// context both match, we go through the streaming form-fill endpoint
// instead of /ask, so the sidepanel can render DIRECT fields as soon
// as they resolve rather than waiting on the Qwen batch.
const FORM_FILL_PATTERNS = [
  /^\s*(please\s+)?fill\s+(this|the)\s+(form|application|page)\s*[.!?]*\s*$/i,
  /^\s*(please\s+)?fill\s+(it|this|the\s+form|the\s+application)\s+out\s*[.!?]*\s*$/i,
  /^\s*(please\s+)?autofill(\s+(this|the))?(\s+(form|application|page))?\s*[.!?]*\s*$/i,
  /^\s*(please\s+)?auto[- ]?fill\s+(this|the)\s+(form|application|page)\s*[.!?]*\s*$/i,
];
function isFormFillQuestion(q) {
  const s = (q || "").trim();
  return FORM_FILL_PATTERNS.some((r) => r.test(s));
}

console.log("[PA DEBUG] background loaded", new Date().toISOString());

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
  console.log("[PA DEBUG][COMMAND]", command);
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
    console.log("[PA DEBUG][BACKGROUND ASK RECEIVED]", { hasQuestion: !!msg.question });
    handleAsk(msg.question)
      .then((r) => { console.log("[PA DEBUG][HANDLE ASK END] ok=", !!r?.ok); sendResponse(r); })
      .catch((err) => {
        console.warn("[PA DEBUG][HANDLE ASK ERROR]", err?.message ?? err);
        sendResponse({ ok: false, error: String(err?.message ?? err) });
      });
    return true;
  }
  if (msg?.type === "INSERT_DRAFT") {
    relayInsertDraft(msg.text ?? "")
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: String(err?.message ?? err) }));
    return true;
  }
  if (msg?.type === "FILL_FIELD") {
    relayFillField({
      tabId: typeof msg.tabId === "number" ? msg.tabId : null,
      frameId: typeof msg.frameId === "number" ? msg.frameId : undefined,
      selector: msg.selector,
      value: msg.value ?? "",
      shadowPath: Array.isArray(msg.shadowPath) ? msg.shadowPath : [],
    })
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: String(err?.message ?? err) }));
    return true;
  }
});

async function handleAsk(question) {
  console.log("[PA DEBUG][HANDLE ASK START]");
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  console.log("[PA DEBUG][HANDLE ASK] tab=", tab?.id, "url=", tab?.url);
  if (!tab?.id) throw new Error("No active tab.");
  if (!isInjectableTab(tab)) {
    throw new Error("Personal Agent cannot access this page. Open a normal http/https page and try again.");
  }

  // Top-frame context (url, title, page_text, gmail thread, youtube). We
  // pin frameId: 0 because content.js now guards GET_PAGE_CONTEXT to only
  // respond in the top frame.
  const ctx = await sendToTabWithContentScriptRetry(
    tab,
    { type: "GET_PAGE_CONTEXT" },
    { frameId: 0 }
  );
  console.log("[PA DEBUG][HANDLE ASK] ctx.ok=", ctx?.ok);
  if (!ctx?.ok) throw new Error(ctx?.error ?? "Failed to read page context.");

  // Aggregate form_fields from EVERY frame in the tab. Most ATS
  // embeds mount the application form in a cross-origin child iframe;
  // top-frame-only detection returns nothing. Each field is annotated
  // with the frameId that produced it so FILL_FIELD can be routed
  // back to the same frame later.
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

  // Progressive-fill path: if this is a form-fill request and fields
  // were detected, stream NDJSON events to the sidepanel so DIRECT
  // fields render immediately without waiting for Qwen.
  if (isFormFillQuestion(question) && formFields.length > 0) {
    return await handleAskFormStream({
      question, context, tabId: tab.id, title: ctx.title,
    });
  }

  console.log("[PA DEBUG][FASTAPI REQUEST START]", SERVER_URL,
              "fields=", formFields.length, "url=", ctx.url);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(SERVER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, context }),
      signal: controller.signal,
    });
  } catch (e) {
    clearTimeout(timeoutId);
    console.warn("[PA DEBUG][FASTAPI ERROR]", e?.name, e?.message);
    if (e?.name === "AbortError") {
      throw new Error(`Backend at ${SERVER_URL} did not respond within ${FETCH_TIMEOUT_MS/1000}s. Is uvicorn running?`);
    }
    throw new Error(`Cannot reach ${SERVER_URL}: ${e?.message ?? e}`);
  } finally {
    clearTimeout(timeoutId);
  }
  console.log("[PA DEBUG][FASTAPI RESPONSE] status=", res.status);
  if (!res.ok) throw new Error(`Server ${res.status}: ${await res.text()}`);
  const data = await res.json();
  return {
    ok: true,
    answer: data.answer,
    trace: data.trace,
    title: ctx.title,
    requires_confirmation: !!data.requires_confirmation,
    draft: data.draft ?? null,
    // Pin the form session to the tab that produced these fields. The
    // sidepanel stores this and echoes it back on every FILL_FIELD, so
    // routing never falls back to chrome.tabs.query — that query fails
    // ("No active tab") when the side panel itself is focused.
    session_tab_id: tab.id,
  };
}

async function handleAskFormStream({ question, context, tabId, title }) {
  console.log("[PA DEBUG][FASTAPI STREAM START]", SERVER_URL_FORM_STREAM,
              "fields=", (context.form_fields || []).length);
  // Kick off the fetch in the background — do NOT await the full body
  // here; the sidepanel needs a synchronous ACK so it can render the
  // meta/direct events as they arrive via runtime.sendMessage.
  (async () => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    let res;
    try {
      res = await fetch(SERVER_URL_FORM_STREAM, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, context }),
        signal: controller.signal,
      });
    } catch (e) {
      clearTimeout(timeoutId);
      chrome.runtime.sendMessage({
        type: "FORM_STREAM_EVENT",
        event: { event: "error", error: `Cannot reach ${SERVER_URL_FORM_STREAM}: ${e?.message ?? e}` },
      }).catch(() => {});
      return;
    }
    if (!res.ok) {
      clearTimeout(timeoutId);
      const errText = await res.text().catch(() => "");
      chrome.runtime.sendMessage({
        type: "FORM_STREAM_EVENT",
        event: { event: "error", error: `Server ${res.status}: ${errText}` },
      }).catch(() => {});
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, idx).trim();
          buffer = buffer.slice(idx + 1);
          if (!line) continue;
          let event;
          try { event = JSON.parse(line); }
          catch (_) { continue; }
          chrome.runtime.sendMessage({
            type: "FORM_STREAM_EVENT",
            event,
          }).catch(() => {});
        }
      }
    } finally {
      clearTimeout(timeoutId);
    }
  })();

  // Synchronous ACK. Sidepanel treats streaming=true as "wait for
  // FORM_STREAM_EVENT messages, don't try to render the response
  // object here".
  return {
    ok: true,
    streaming: true,
    session_tab_id: tabId,
    title: title,
    trace: [{ tool: "detect_form_fields", streaming: true }],
  };
}

async function relayInsertDraft(text) {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab?.id) return { ok: false, error: "No active tab." };
  if (!isInjectableTab(tab)) {
    return { ok: false, error: "Personal Agent cannot access this page." };
  }
  return await sendToTabWithContentScriptRetry(tab, { type: "INSERT_DRAFT", text });
}

async function relayFillField({ tabId, frameId, selector, value, shadowPath }) {
  console.log(
    "[FORM DEBUG][FILL]",
    JSON.stringify({
      tabId,
      frameId,
      selector,
      hasShadowPath: Array.isArray(shadowPath) && shadowPath.length > 0,
      valuePresent: value !== undefined && value !== null && String(value).length > 0,
    })
  );
  if (typeof tabId !== "number") {
    return { ok: false, error: "Fill request missing tabId — form session lost." };
  }
  // Validate the tab still exists. If the user closed the application
  // tab while reviewing the sidebar, say so plainly rather than
  // silently retargeting a different tab.
  try {
    await chrome.tabs.get(tabId);
  } catch (_) {
    return { ok: false, error: "Application tab was closed." };
  }
  const tab = await chrome.tabs.get(tabId);
  if (!isInjectableTab(tab)) {
    return { ok: false, error: "Personal Agent cannot access this page." };
  }
  const opts = typeof frameId === "number" ? { frameId } : {};
  return await sendToTabWithContentScriptRetry(
    tab,
    { type: "FILL_FIELD", selector, value, shadowPath: shadowPath || [] },
    opts
  );
}

async function collectFormFieldsAllFrames(tabId) {
  // Runs detectFormFields() in every frame of the tab (content scripts
  // are declared with all_frames: true), aggregating results and
  // annotating each field with the producing frameId.
  //
  // Retry policy: modern job forms mount async. If the first pass
  // returns zero fields we wait 250 ms and try again; if still zero
  // we wait another 500 ms (total ceiling 750 ms). No open-ended
  // polling — either the form is present within that window or the
  // user's next action re-triggers detection.
  const delays = [0, 250, 500];
  let out = [];
  for (let attempt = 0; attempt < delays.length; attempt++) {
    const wait = delays[attempt];
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    console.log(
      `[FORM DEBUG][ATTEMPT] tabId=${tabId} attempt=${attempt + 1}/${delays.length} after_wait_ms=${wait}`
    );
    out = await scanAllFramesOnce(tabId);
    console.log(
      `[FORM DEBUG][ATTEMPT_RESULT] attempt=${attempt + 1} merged_fields=${out.length}`
    );
    if (out.length > 0) break;
  }
  console.log(`[FORM DEBUG][MERGE] final_merged_fields=${out.length}`);
  return out;
}

async function scanAllFramesOnce(tabId) {
  // Injected probe wraps detectFormFields() so we can distinguish:
  //   - detectFormFields undefined (content script not loaded in frame)
  //   - detectFormFields threw
  //   - detectFormFields returned non-array
  //   - detectFormFields returned []
  // All four look identical to the outer merge loop otherwise.
  let raw;
  try {
    raw = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const probe = {
          href: typeof location !== "undefined" ? location.href : "?",
          hasFn: typeof detectFormFields === "function",
          isTop: typeof window !== "undefined" ? window === window.top : null,
          error: null,
          resultType: null,
          isArray: false,
          length: 0,
          fields: [],
        };
        try {
          if (probe.hasFn) {
            const r = detectFormFields();
            probe.resultType = r === null ? "null" : typeof r;
            probe.isArray = Array.isArray(r);
            probe.length = Array.isArray(r) ? r.length : -1;
            probe.fields = Array.isArray(r) ? r : [];
          }
        } catch (e) {
          probe.error = String((e && e.message) || e);
        }
        return probe;
      },
    });
  } catch (e) {
    console.warn(
      `[FORM DEBUG][EXECUTE RAW] executeScript THREW — tabId=${tabId} error=`, e
    );
    return [];
  }

  const summary = Array.isArray(raw) ? {
    entries: raw.length,
    per_frame: raw.map((r) => ({
      frameId: r?.frameId,
      documentId: r?.documentId,
      isTop: r?.result?.isTop,
      href: r?.result?.href,
      hasFn: r?.result?.hasFn,
      resultType: r?.result?.resultType,
      isArray: r?.result?.isArray,
      length: r?.result?.length,
      error: r?.result?.error ?? null,
      injection_error: r?.error ?? null,
    })),
  } : { entries: 0, note: "executeScript returned non-array", raw: String(raw) };
  console.log("[FORM DEBUG][EXECUTE RAW]", JSON.stringify(summary, null, 2));

  const out = [];
  const perFrame = {};
  for (const r of raw || []) {
    const frameId = r?.frameId;
    const probe = r?.result;
    if (!probe || !Array.isArray(probe.fields)) continue;
    perFrame[frameId] = probe.fields.length;
    for (const f of probe.fields) {
      f.frameId = frameId;
      out.push(f);
    }
  }
  console.log("[FORM DEBUG][MERGE] per_frame=", perFrame, "merged=", out.length);
  return out;
}

function isInjectableTab(tab) {
  const url = tab?.url || "";
  return /^(https?|file):/i.test(url);
}

function isReceivingEndError(err) {
  return String(err?.message || err || "").includes(RECEIVING_END_ERROR);
}

function pageAccessError() {
  return new Error("Personal Agent could not access this page. Refresh the page and try again, or open a normal website page.");
}

async function ensureContentScripts(tabId) {
  console.log("[Background] Injecting content scripts into tab:", tabId);
  await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    files: CONTENT_SCRIPT_FILES,
  });
}

async function sendToTabWithContentScriptRetry(tab, message, options) {
  console.log("[Background] Sending message to tab:", {
    tabId: tab.id,
    url: tab.url,
    title: tab.title,
    type: message?.type,
    frameId: options?.frameId,
  });
  await ensureTabReceiver(tab, options);
  return await sendToTab(tab.id, message, options);
}

async function ensureTabReceiver(tab, options) {
  try {
    const ping = await sendToTab(tab.id, { type: "PA_PING" }, options);
    if (ping?.ok) return ping;
  } catch (err) {
    if (!isReceivingEndError(err)) throw err;
  }
  try {
    await ensureContentScripts(tab.id);
  } catch (injectErr) {
    console.warn("[Background] Content script injection failed:", injectErr?.message || injectErr);
    throw pageAccessError();
  }
  try {
    const ping = await sendToTab(tab.id, { type: "PA_PING" }, options);
    if (ping?.ok) return ping;
  } catch (retryErr) {
    if (isReceivingEndError(retryErr)) {
      throw pageAccessError();
    }
    throw retryErr;
  }
  throw pageAccessError();
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

console.log("[PA DEBUG] listeners registered");
