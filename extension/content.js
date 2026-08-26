// Content script: on request, return the current selection (if any) or the
// visible innerText of the page. Truncation happens on the server.

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type !== "GET_PAGE_TEXT") return;
  try {
    const selection = window.getSelection()?.toString() ?? "";
    const text = selection.trim().length > 0
      ? selection
      : (document.body?.innerText ?? "");
    sendResponse({ ok: true, text, url: location.href, title: document.title });
  } catch (e) {
    sendResponse({ ok: false, error: String(e) });
  }
  return true;
});
