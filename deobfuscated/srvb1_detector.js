/**
 * srvb1.com Ad Reinsertion Detector — Deobfuscated
 *
 * Source: srvb1.com/o.js?uid=3de5c6e969bcbef63f8b7a59 (~2.8KB)
 *
 * "OpenAd"-family recovery service. Uses differential image loading
 * to detect ad blockers, then injects replacement banners.
 */

(function () {
  const CONFIG = {
    uid: "3de5c6e969bcbef63f8b7a59",
    selector: ".adv.banner-mpu > div",  // GSMArena's actual ad container
    autoCenter: true,
    flagClass: "__openad__flag__multi__ab__",
    sentinelId: "FM44WOtNU0",
  };

  /**
   * Differential bait detection.
   *
   * Loads two 1x1 pixel GIFs from srvb1.com:
   *   ch=1 — "control" (designed to pass most filter lists)
   *   ch=2 — "probe" (designed to be caught by ad filters)
   *
   * BOTH return HTTP 200 + image/gif server-side.
   * The ONLY way ch=2 fails while ch=1 loads is a client filter rule.
   *
   * Logic:
   *   ch=1 ok  + ch=2 fail = ADBLOCK DETECTED
   *   ch=1 ok  + ch=2 ok   = clean
   *   ch=1 fail + ch=2 fail = network issue (NOT adblock — avoids false positives)
   */
  function detect(baseUrl, callback) {
    const rn = Math.random();
    let controlLoaded = false;
    let probeBlocked = false;
    let done = false;

    function check() {
      if (done) return;
      // Adblock = control loaded AND probe blocked
      const adblockDetected = controlLoaded && probeBlocked;
      done = true;
      callback(adblockDetected);
    }

    // Control image (ch=1)
    const control = new Image();
    control.onload = function () {
      controlLoaded = true;
      // Small delay then check
      setTimeout(check, 250);
    };
    control.onerror = function () {
      controlLoaded = false;
      setTimeout(check, 250);
    };
    control.src = baseUrl + "?ch=1&rn=" + rn;

    // Probe image (ch=2) — this is the one filter lists catch
    const probe = new Image();
    probe.onload = function () {
      probeBlocked = false;
      setTimeout(check, 250);
    };
    probe.onerror = function () {
      probeBlocked = true;
      setTimeout(check, 250);
    };
    probe.src = baseUrl + "?ch=2&rn=" + rn;

    // Timeout fallback
    setTimeout(check, 1000);
  }

  /**
   * On adblock detected: inject replacement banner.
   */
  function injectBanner(container) {
    const banner = document.createElement("a");
    banner.href = "https://www.bestprice.com/";  // recovery ad destination
    banner.target = "_blank";
    banner.rel = "noopener";

    const img = document.createElement("img");
    img.src =
      "https://srvb1.com/banner/" +
      CONFIG.uid +
      ".jpg?uid=" +
      CONFIG.uid +
      "&puburl=" +
      encodeURIComponent(window.location.href);
    img.style.maxWidth = "300px";

    banner.appendChild(img);

    // "Ad" label
    const label = document.createElement("span");
    label.textContent = "Ad";
    label.style.cssText =
      "position:absolute;top:0;right:0;font-size:10px;background:#eee;padding:1px 4px;";

    const wrapper = document.createElement("div");
    wrapper.style.position = "relative";
    wrapper.appendChild(banner);
    wrapper.appendChild(label);

    // Find the ad container using configured selector + offsetParent visibility check
    const el = document.querySelector(CONFIG.selector);
    if (el && el.offsetParent !== null) {
      el.innerHTML = "";
      el.appendChild(wrapper);
    }
  }

  /**
   * On clean: fingerprint client and load stage-2.
   */
  function loadStage2() {
    // FNV-1a hash of device fingerprint
    const fingerprint = [
      screen.width,
      screen.height,
      screen.colorDepth,
      window.devicePixelRatio,
      navigator.hardwareConcurrency,
      navigator.deviceMemory,
      Intl.DateTimeFormat().resolvedOptions().timeZone,
    ].join("|");

    const sessId = fnv1aHash(fingerprint);

    const script = document.createElement("script");
    script.src =
      "https://srvb1.com/?uid=66ec1d33e2e1bc525255206b&oab=1" +
      "&puburl=" + encodeURIComponent(window.location.href) +
      "&sessId=" + sessId;
    document.body.appendChild(script);
  }

  function fnv1aHash(str) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      hash ^= str.charCodeAt(i);
      hash = (hash * 0x01000193) >>> 0;
    }
    return hash.toString(16);
  }

  // === RUN ===
  detect("https://srvb1.com/px.gif", function (adblockDetected) {
    if (adblockDetected) {
      injectBanner();
    } else {
      loadStage2();
    }
  });
})();
