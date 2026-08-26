// Gmail DOM extraction + compose insertion.
// Loaded as a content script on every URL (via manifest content_scripts) and
// also sourceable from extension/fixtures/test.html for offline testing.
// Nothing here has side effects until insertIntoOpenCompose(text) is called.
//
// Load-bearing selectors (also listed in CLAUDE.md so breakage is diagnosable):
//   h2.hP                                         — thread subject
//   .adn                                          — one node per message
//   .gD                                           — sender chip (name + email= attr)
//   .g3                                           — timestamp chip
//   .a3s.aiL                                      — message body
//   div[aria-label="Message Body"][contenteditable="true"]  — open compose body

(function (root) {
  function extractGmailThread(scope) {
    scope = scope || document;
    const subjectEl = scope.querySelector("h2.hP");
    const msgEls = scope.querySelectorAll(".adn");
    if (!subjectEl || msgEls.length === 0) return null;

    const messages = [];
    const participants = new Map();
    for (const el of msgEls) {
      const senderEl = el.querySelector(".gD");
      const tsEl = el.querySelector(".g3");
      const bodyEl = el.querySelector(".a3s.aiL");
      if (!senderEl || !bodyEl) continue;

      const from = (senderEl.getAttribute("name") || senderEl.textContent || "").trim();
      const email = (senderEl.getAttribute("email") || "").trim();
      const timestamp = tsEl
        ? (tsEl.getAttribute("title") || tsEl.textContent || "").trim()
        : "";
      const body_text = normalizeText(bodyEl);

      messages.push({ from, email, timestamp, body_text });
      if (email && !participants.has(email)) {
        participants.set(email, { name: from, email });
      }
    }

    if (messages.length === 0) return null;

    return {
      subject: (subjectEl.textContent || "").trim(),
      participants: Array.from(participants.values()),
      messages,
    };
  }

  function normalizeText(el) {
    // innerText collapses <br> to \n; strip trailing whitespace per line.
    const raw = el.innerText || el.textContent || "";
    return raw
      .split("\n")
      .map((line) => line.replace(/\s+$/g, ""))
      .join("\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function insertIntoOpenCompose(text) {
    const composeBody = document.querySelector(
      'div[aria-label="Message Body"][contenteditable="true"]'
    );
    if (!composeBody) {
      return {
        ok: false,
        error: "No open compose window found. Open a reply/compose in Gmail first.",
      };
    }
    // Focus and use execCommand so Gmail's own input listeners fire — plain
    // innerText assignment leaves the compose thinking the body is empty.
    composeBody.focus();
    try {
      // Move caret to end.
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(composeBody);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand("insertText", false, text);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e?.message ?? e) };
    }
  }

  root.extractGmailThread = extractGmailThread;
  root.insertIntoOpenCompose = insertIntoOpenCompose;
})(typeof globalThis !== "undefined" ? globalThis : window);
