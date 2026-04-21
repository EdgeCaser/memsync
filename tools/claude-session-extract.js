/**
 * claude-session-extract.js
 *
 * Bookmarklet that extracts a Claude Code remote session transcript from
 * claude.ai/code and sends it to the memsync daemon capture endpoint.
 *
 * Usage (bookmarklet):
 *   1. Minify this file and wrap in javascript:(function(){...})()
 *   2. Save as a bookmark in Safari/Chrome
 *   3. Open a claude.ai/code session page
 *   4. Tap the bookmark
 *
 * Usage (console):
 *   Paste into browser devtools console on a claude.ai/code session page.
 *
 * Configuration:
 *   Set MEMSYNC_CAPTURE_URL below, or leave empty to copy to clipboard only.
 *   Set MEMSYNC_TOKEN if your capture endpoint requires auth.
 */

(function () {
  "use strict";

  // ── Configuration ─────────────────────────────────────────────────
  var MEMSYNC_CAPTURE_URL = ""; // e.g. "http://pi.local:5001/note"
  var MEMSYNC_TOKEN = "";       // X-Memsync-Token value, or empty

  // ── Expand truncated messages ─────────────────────────────────────
  var showMoreBtns = Array.from(document.querySelectorAll("button")).filter(
    function (b) { return b.textContent.trim() === "Show more"; }
  );

  if (showMoreBtns.length > 0) {
    showMoreBtns.forEach(function (b) { b.click(); });
    // Wait for DOM to update after expanding, then extract
    setTimeout(extractAndDeliver, 800);
  } else {
    extractAndDeliver();
  }

  function extractAndDeliver() {
    // ── Extract messages ────────────────────────────────────────────
    var containers = document.querySelectorAll('[class*="group/message"]');
    if (!containers.length) {
      window.alert("No messages found. Are you on a claude.ai/code session page?");
      return;
    }

    var turns = [];
    containers.forEach(function (el) {
      // User messages: parent has "items-end" + "ml-auto" (right-aligned)
      var parent = el.parentElement;
      var isUser = parent
        && /items-end/.test(parent.className)
        && /ml-auto/.test(parent.className);
      var role = isUser ? "USER" : "ASSISTANT";

      // Get clean text, collapsing whitespace runs
      // Strip "Copy message" and "Copy code" button text artifacts
      var text = el.textContent
        .replace(/Copy message/g, "")
        .replace(/Copy code/g, "")
        .replace(/Show more/g, "")
        .replace(/\s+/g, " ")
        .trim();

      if (!text) return;

      turns.push("[" + role + "]\n" + text);
    });

    if (!turns.length) {
      window.alert("Could not extract any messages from this page.");
      return;
    }

    // ── Build transcript ────────────────────────────────────────────
    var urlMatch = window.location.pathname.match(/session_([A-Za-z0-9]+)/);
    var sessionId = urlMatch ? urlMatch[1] : "unknown";
    var timestamp = new Date().toISOString().slice(0, 19).replace("T", " ");

    var header = "# Claude Code remote session transcript\n"
      + "# Extracted: " + timestamp + "\n"
      + "# Session: " + sessionId + "\n"
      + "# URL: " + window.location.href + "\n"
      + "# Messages: " + turns.length + "\n\n";

    var transcript = header + turns.join("\n\n---\n\n");

    // ── Deliver ─────────────────────────────────────────────────────
    if (MEMSYNC_CAPTURE_URL) {
      var headers = { "Content-Type": "application/json" };
      if (MEMSYNC_TOKEN) {
        headers["X-Memsync-Token"] = MEMSYNC_TOKEN;
      }
      fetch(MEMSYNC_CAPTURE_URL, {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ text: transcript }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            window.alert(
              "memsync: captured " + turns.length + " messages (" + data.timestamp + ")"
            );
          } else {
            throw new Error(data.error || "unknown error");
          }
        })
        .catch(function (err) {
          copyAndNotify(
            transcript, turns.length,
            "POST failed (" + err.message + "), copied to clipboard instead."
          );
        });
    } else {
      copyAndNotify(
        transcript, turns.length,
        "Copied " + turns.length + " messages to clipboard.\n"
          + "Paste into: memsync refresh --file -"
      );
    }
  }

  function copyAndNotify(text, count, message) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        window.alert("memsync: " + message);
      }).catch(function () {
        showCopyFallback(text, count);
      });
    } else {
      showCopyFallback(text, count);
    }
  }

  function showCopyFallback(text, count) {
    var overlay = document.createElement("div");
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,0.7);"
      + "display:flex;align-items:center;justify-content:center;padding:20px;";

    var box = document.createElement("div");
    box.style.cssText =
      "background:#1a1a2e;color:#e0e0e0;border-radius:12px;padding:20px;"
      + "max-width:600px;width:100%;max-height:80vh;"
      + "display:flex;flex-direction:column;gap:12px;font-family:system-ui;";

    var title = document.createElement("div");
    title.style.cssText = "font-size:16px;font-weight:600;";
    title.textContent = "memsync: " + count + " messages extracted";

    var hint = document.createElement("div");
    hint.style.cssText = "font-size:13px;color:#999;";
    hint.textContent = "Select all and copy, then run: memsync refresh --file <paste>";

    var ta = document.createElement("textarea");
    ta.style.cssText =
      "width:100%;height:300px;background:#0d0d1a;color:#c0c0c0;"
      + "border:1px solid #333;border-radius:8px;padding:12px;"
      + "font-family:monospace;font-size:12px;resize:none;";
    ta.value = text;
    ta.readOnly = true;

    var closeBtn = document.createElement("button");
    closeBtn.style.cssText =
      "align-self:flex-end;background:#333;color:#fff;border:none;"
      + "border-radius:6px;padding:8px 16px;cursor:pointer;font-size:14px;";
    closeBtn.textContent = "Close";
    closeBtn.onclick = function () { overlay.remove(); };

    box.appendChild(title);
    box.appendChild(hint);
    box.appendChild(ta);
    box.appendChild(closeBtn);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    ta.focus();
    ta.select();
  }
})();
