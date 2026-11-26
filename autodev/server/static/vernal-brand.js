/* Lightweight visible-brand normalizer for embedded assistant/chat frames.
 * Only rewrites human-visible brand copy; never touches code blocks, inputs, or APIs.
 */
(function () {
  "use strict";
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "CODE", "PRE", "TEXTAREA", "INPUT"]);
  const BRAND_RE = /\bLoopForge\b/gi;

  function rewriteText(value) {
    return typeof value === "string" && BRAND_RE.test(value)
      ? value.replace(BRAND_RE, "Vernal")
      : value;
  }

  function scrubElement(root) {
    if (!root) return;
    if (document.title && BRAND_RE.test(document.title)) {
      document.title = rewriteText(document.title);
    }
    if (root.nodeType === Node.TEXT_NODE) {
      const parent = root.parentElement;
      if (parent && !SKIP_TAGS.has(parent.tagName)) {
        const next = rewriteText(root.nodeValue);
        if (next !== root.nodeValue) root.nodeValue = next;
      }
      return;
    }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    if (SKIP_TAGS.has(root.tagName)) return;
    for (const attr of ["placeholder", "aria-label", "title"]) {
      const cur = root.getAttribute?.(attr);
      if (cur && BRAND_RE.test(cur)) {
        root.setAttribute(attr, rewriteText(cur));
      }
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      const p = node.parentElement;
      if (p && !SKIP_TAGS.has(p.tagName)) {
        const next = rewriteText(node.nodeValue);
        if (next !== node.nodeValue) node.nodeValue = next;
      }
      node = walker.nextNode();
    }
  }

  function start() {
    scrubElement(document.body);
    if (!document.body || typeof MutationObserver !== "function") return;
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type === "characterData") {
          scrubElement(m.target);
        } else {
          for (const added of m.addedNodes) scrubElement(added);
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
