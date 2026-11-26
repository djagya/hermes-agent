"""Which captcha a page shows, read inside the vault's own write script (no extra browser round trip), so a
fill or code result can tell the agent why a submit may do nothing (J-0643: four "Log in" clicks on a page
with a reCAPTCHA frame, then a handoff). Owner D-1084 a (2026-11-26): text captchas are read with vision and
typed; puzzle captchas stay a handoff.

The page answers one fixed code; Python accepts only the codes below and never echoes page text.
"""

from __future__ import annotations

from typing import Any

CHALLENGES = {
    "captcha_text": ("The page shows a text captcha (distorted characters in an image). Take a screenshot of it, "
                     "read the characters with vision, type them into its box with browser tools and submit once; "
                     "after one wrong answer, try another route or send the user a browser_control handoff."),
    "captcha_puzzle": ("The page shows a puzzle captcha (reCAPTCHA or hCaptcha checkbox or image grid, Cloudflare "
                       "Turnstile). Do not click submit again and again: wait a few seconds for it to clear by "
                       "itself, then send the user a browser_control handoff for this tab."),
    "captcha_invisible": ("The site runs an invisible bot check (reCAPTCHA badge). Submit once; if nothing "
                          "happens, the check blocked this browser: do not click again; sign in from the "
                          "persistent shopping slot, or send the user a browser_control handoff."),
}

# A JS function expression answering "captcha_puzzle", "captcha_text", "captcha_invisible" or "" for the top
# document: a shown reCAPTCHA / hCaptcha / Turnstile frame (an invisible reCAPTCHA's badge counts as
# invisible until its challenge frame shows), else a shown image, input or canvas named like a captcha.
CAPTCHA_JS = r"""(() => {
  try {
    const shown = (e) => {
      const r = e.getBoundingClientRect(), st = getComputedStyle(e);
      return r.width >= 20 && r.height >= 20 && r.right > 0 && r.bottom > 0 && r.left < (window.innerWidth || 0) &&
        st.display !== "none" && st.visibility !== "hidden" && parseFloat(st.opacity || "1") > 0.05;
    };
    let puzzle = false, invisible = false, text = false;
    for (const f of document.querySelectorAll("iframe")) {
      let u = null;
      try { u = new URL(f.getAttribute("src") || "about:blank", document.baseURI); } catch (_) { continue; }
      const host = u.hostname;
      const recaptcha = /(^|\.)(google\.com|recaptcha\.net)$/.test(host) && u.pathname.includes("/recaptcha/");
      const vendor = recaptcha || /(^|\.)hcaptcha\.com$/.test(host) || host === "challenges.cloudflare.com";
      if (!vendor || !shown(f)) continue;
      if (recaptcha && /(^|&)size=invisible(&|$)/.test(u.search.slice(1))) invisible = true;
      else puzzle = true;
    }
    if (!puzzle) {
      for (const e of document.querySelectorAll("img, input, canvas")) {
        const words = ["id", "class", "name", "alt", "src", "placeholder", "aria-label"]
          .map((a) => e.getAttribute(a) || "").join(" ");
        if (/captcha/i.test(words) && shown(e)) { text = true; break; }
      }
    }
    return puzzle ? "captcha_puzzle" : text ? "captcha_text" : invisible ? "captcha_invisible" : "";
  } catch (_) { return ""; }
})"""


def challenge_fields(parsed: Any) -> dict[str, str]:
    """``{"challenge": code, "challenge_next": advice}`` when the write script saw a captcha, else ``{}``."""
    code = parsed.get("challenge") if isinstance(parsed, dict) else None
    return {"challenge": code, "challenge_next": CHALLENGES[code]} if isinstance(code, str) and code in CHALLENGES else {}
