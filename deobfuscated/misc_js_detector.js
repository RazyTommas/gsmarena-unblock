/**
 * GSMArena Native Ad-Block Detector — Deobfuscated
 *
 * Source: fdn.gsmarena.com/vv/assets12/js/misc.js (v=155)
 * Location: Last DOMContentLoaded block, lines ~1143-1169 (beautified)
 *
 * This is the annotated, readable version of the obfuscated detection code.
 * Original uses base64 strings + char-code-built atob to evade filters.
 */

// Line 1 of misc.js — decoy global to confuse anti-detection scripts
window.suggestmeyes_loaded = true;

// ... (rest of misc.js — $gsm utilities, page logic, etc.) ...

// === AD-BLOCK DETECTION (DOMContentLoaded handler) ===

document.addEventListener("DOMContentLoaded", function () {
  // Guard: only run if ad header slots exist on the page
  const subHeader2 = document.getElementById("subHeader2");
  const subHeader3 = document.getElementById("subHeader3");

  if (!subHeader2 && !subHeader3) return;

  // Original: const n = window[[String.fromCharCode(97,116,111,98)].join("")];
  const atobFn = window.atob;

  /**
   * Check if a bait element was hidden by cosmetic filters.
   * Returns a Promise<boolean> — true if ad-blocker detected.
   */
  function checkBaitElement(element) {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve(element.offsetHeight === 0);
      }, 100);
    });
  }

  /**
   * Main detection function.
   * 1. Creates a bait div with ad-like class names
   * 2. Checks if cosmetic filters hid it (offsetHeight === 0)
   * 3. Falls back to a network probe (doubleclick favicon fetch)
   */
  function detect() {
    // Original: all strings are atob("...base64...")
    const baitClasses =
      "pub_300x250 pub_300x250m pub_728x90 text-ad textAd " +
      "text-ad-links ad-unit adv-mpu stickyads ads_banner " +
      "AdBox text-ad-top";

    const baitStyle =
      "width: 1px !important; " +
      "height: 1px !important; " +
      "position: absolute !important; " +
      "left: -10000px !important; " +
      "top: -1000px !important;";

    // Create bait element — ad blockers' cosmetic filters target these classes
    const bait = $gsm.tag("div", { class: baitClasses, style: baitStyle }, "", document.body);

    // Check 1: cosmetic filter detection
    return checkBaitElement(bait).then(function (isHidden) {
      if (isHidden) return true; // Cosmetic filter detected — adblock confirmed

      // Check 2: network filter detection (fallback)
      // Original: atob("aHR0cHM6Ly9hZC5kb3VibGVjbGljay5uZXQvZmF2aWNvbi5pY28=")
      return fetch("https://ad.doubleclick.net/favicon.ico")
        .then(function (response) {
          // Original: atob("Q29udGVudC1UeXBl") = "Content-Type"
          // Original: atob("aW1hZ2UveC1pY29u") = "image/x-icon"
          return response.headers.get("Content-Type") !== "image/x-icon";
        })
        .catch(function () {
          return true; // Fetch blocked = adblock confirmed
        });
    });
  }

  /**
   * Encode detection result in the DeviceID cookie.
   *
   * The PARITY of the value is the signal:
   *   ODD  = ad-blocker detected
   *   EVEN = clean (no ad-blocker)
   *
   * The cookie looks like a random device fingerprint to casual inspection.
   * Only the server knows to check (value % 2).
   */
  function encodeResult(adblockDetected) {
    // Generate a random even base: 10000, 10002, 10004, ..., 11998
    const base = 10000 + 2 * Math.round(999 * Math.random());

    // Original: atob("RGV2aWNlSUQ=") = "DeviceID"
    const cookieValue = adblockDetected ? base + 1 : base + 0;

    // Set cookie with 7-day TTL
    $gsm.setCookie("DeviceID", cookieValue, 7);

    // Re-arm on beforeunload — persists the flag across navigation
    if (adblockDetected) {
      window.addEventListener("beforeunload", function () {
        const freshBase = 10000 + 2 * Math.round(999 * Math.random());
        $gsm.setCookie("DeviceID", freshBase + 1, 7);
      });
    }
  }

  // === RUN DETECTION ===
  detect().then(encodeResult);
});
