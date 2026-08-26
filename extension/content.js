// Content script: on request, return a context bundle for the active tab.
// - Non-YouTube: selection (if any) or document.body.innerText, as in Phase 0.
// - youtube.com/watch: video_id + parsed caption track from
//   ytInitialPlayerResponse. If captions aren't available, transcript is null;
//   the server surfaces that as "no transcript available" rather than guessing.

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "GET_PAGE_CONTEXT") return;
  buildContext()
    .then((ctx) => sendResponse({ ok: true, ...ctx }))
    .catch((e) => sendResponse({ ok: false, error: String(e?.message ?? e) }));
  return true; // async
});

async function buildContext() {
  const url = location.href;
  const title = document.title;
  const isYoutube =
    location.hostname.endsWith("youtube.com") && location.pathname === "/watch";

  if (!isYoutube) {
    const selection = window.getSelection()?.toString() ?? "";
    const page_text =
      selection.trim().length > 0 ? selection : (document.body?.innerText ?? "");
    return {
      url,
      title,
      page_text,
      is_youtube: false,
      video_id: null,
      transcript: null,
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
  };
}

async function tryGetYoutubeTranscript() {
  try {
    const player = findPlayerResponse();
    const tracks =
      player?.captions?.playerCaptionsTracklistRenderer?.captionTracks ?? [];
    if (!tracks.length) return null;

    // Prefer an English track; otherwise take the first.
    const track =
      tracks.find((t) => (t.languageCode || "").toLowerCase().startsWith("en")) ||
      tracks[0];
    if (!track?.baseUrl) return null;

    // Ask for JSON3 — easier to parse than the default XML.
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
  // Fast path: YouTube leaves ytInitialPlayerResponse on window in some
  // contexts, but content scripts run in an isolated world, so read from the
  // inline scripts instead.
  const scripts = document.querySelectorAll("script");
  for (const s of scripts) {
    const src = s.textContent || "";
    const marker = "ytInitialPlayerResponse = ";
    const idx = src.indexOf(marker);
    if (idx === -1) continue;
    const start = idx + marker.length;
    // Find the matching closing brace by walking depth. YouTube ends the
    // object with `};` or `;var`, so a bounded scan is enough.
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
