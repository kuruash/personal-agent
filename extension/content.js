// Content script: on request, return a context bundle for the active tab.
// - Non-YouTube, non-Gmail: selection (if any) or document.body.innerText.
// - youtube.com/watch: video_id + parsed caption track (see Phase 1).
// - mail.google.com: attach extracted email_thread via extractGmailThread()
//   from gmail.js (loaded alongside this script by the manifest).

// Per-frame identification log so we can see, in DevTools, exactly where
// the visible form controls live when detection returns empty.
try {
  console.log(
    "[FORM DEBUG] frame:",
    window === window.top ? "TOP" : "CHILD",
    location.href,
    "inputs=", document.querySelectorAll("input").length,
    "textareas=", document.querySelectorAll("textarea").length,
    "selects=", document.querySelectorAll("select").length
  );
} catch (_) {}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "GET_PAGE_CONTEXT") {
    // GET_PAGE_CONTEXT is a top-frame concern (url / title / page_text /
    // gmail / youtube). Form fields are gathered separately via
    // scripting.executeScript across ALL frames — see background.js. This
    // guard prevents child-frame responses from racing the top-frame one
    // when the caller doesn't pin a frameId.
    if (window !== window.top) return false;
    buildContext()
      .then((ctx) => {
        try {
          console.log(
            "[FORM DEBUG] buildContext form_fields:",
            ctx?.form_fields?.length ?? 0,
            ctx?.form_fields
          );
        } catch (_) {}
        sendResponse({ ok: true, ...ctx });
      })
      .catch((e) => sendResponse({ ok: false, error: String(e?.message ?? e) }));
    return true;
  }
  if (msg?.type === "INSERT_DRAFT") {
    try {
      // insertIntoOpenCompose is provided by gmail.js.
      const result = (typeof insertIntoOpenCompose === "function")
        ? insertIntoOpenCompose(msg.text ?? "")
        : { ok: false, error: "gmail.js not loaded on this page." };
      sendResponse(result);
    } catch (e) {
      sendResponse({ ok: false, error: String(e?.message ?? e) });
    }
    return true;
  }
  if (msg?.type === "FILL_FIELD") {
    try {
      // fillField is provided by formdetect.js.
      const result = (typeof fillField === "function")
        ? fillField(msg.selector, msg.value ?? "")
        : { ok: false, error: "formdetect.js not loaded on this page." };
      sendResponse(result);
    } catch (e) {
      sendResponse({ ok: false, error: String(e?.message ?? e) });
    }
    return true;
  }
});

async function buildContext() {
  const url = location.href;
  const title = document.title;
  const isYoutube =
    location.hostname.endsWith("youtube.com") && location.pathname === "/watch";
  const isGmail = location.hostname === "mail.google.com";

  if (isGmail) {
    const email_thread =
      typeof extractGmailThread === "function" ? extractGmailThread() : null;
    return {
      url,
      title,
      page_text: "",
      is_youtube: false,
      video_id: null,
      transcript: null,
      email_thread,
    };
  }

  if (!isYoutube) {
    const selection = window.getSelection()?.toString() ?? "";
    const page_text =
      selection.trim().length > 0 ? selection : (document.body?.innerText ?? "");
    const form_fields =
      typeof detectFormFields === "function" ? detectFormFields() : null;
    return {
      url,
      title,
      page_text,
      is_youtube: false,
      video_id: null,
      transcript: null,
      email_thread: null,
      form_fields: form_fields && form_fields.length > 0 ? form_fields : null,
    };
  }

  const video_id = new URLSearchParams(location.search).get("v");
  const transcript = await tryGetYoutubeTranscript();
  return {
    url,
    title,
    page_text: "",
    is_youtube: true,
    video_id,
    transcript,
    email_thread: null,
  };
}

async function tryGetYoutubeTranscript() {
  try {
    const player = findPlayerResponse();
    const tracks =
      player?.captions?.playerCaptionsTracklistRenderer?.captionTracks ?? [];
    if (!tracks.length) return null;

    const track =
      tracks.find((t) => (t.languageCode || "").toLowerCase().startsWith("en")) ||
      tracks[0];
    if (!track?.baseUrl) return null;

    const url = track.baseUrl + (track.baseUrl.includes("fmt=") ? "" : "&fmt=json3");
    const res = await fetch(url, { credentials: "include" });
    if (!res.ok) return null;
    const data = await res.json();
    return (data.events || [])
      .filter((ev) => Array.isArray(ev.segs))
      .map((ev) => ({
        start: (ev.tStartMs ?? 0) / 1000,
        text: ev.segs.map((s) => s.utf8 || "").join("").replace(/\n/g, " ").trim(),
      }))
      .filter((e) => e.text.length > 0);
  } catch {
    return null;
  }
}

function findPlayerResponse() {
  const scripts = document.querySelectorAll("script");
  for (const s of scripts) {
    const src = s.textContent || "";
    const marker = "ytInitialPlayerResponse = ";
    const idx = src.indexOf(marker);
    if (idx === -1) continue;
    const start = idx + marker.length;
    let depth = 0, inStr = false, esc = false, end = -1;
    for (let i = start; i < src.length; i++) {
      const ch = src[i];
      if (inStr) {
        if (esc) { esc = false; continue; }
        if (ch === "\\") { esc = true; continue; }
        if (ch === '"') inStr = false;
      } else {
        if (ch === '"') inStr = true;
        else if (ch === "{") depth++;
        else if (ch === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
      }
    }
    if (end === -1) continue;
    try {
      return JSON.parse(src.slice(start, end));
    } catch {
      continue;
    }
  }
  return null;
}
