# GSMArena Ad-Block Detection — Technical Analysis

Full reverse-engineering of GSMArena's ad-block detection as of September 2026.

## Architecture

Three independent client-side detectors run in parallel. All funnel into server-side enforcement (HTTP 429). There is **no client-side overlay or paywall**.

## System 1: Native Detector (`misc.js`)

**File:** `fdn.gsmarena.com/vv/assets12/js/misc.js` (v=155, ~36KB)

**Guard:** Only runs if `#subHeader2` or `#subHeader3` exists on the page.

### Obfuscation

The code builds `"atob"` from char codes to evade uBO scriptlet filters:

```js
const n = window[[String.fromCharCode(97,116,111,98)].join("")];  // window["atob"]
```

All detection strings are base64-encoded:

| Base64 | Decoded |
|--------|---------|
| `cHViXzMwMHgyNTAg...` | `pub_300x250 pub_300x250m pub_728x90 text-ad textAd text-ad-links ad-unit adv-mpu stickyads ads_banner AdBox text-ad-top` |
| `d2lkdGg6IDFweCAh...` | `width: 1px !important; height: 1px !important; position: absolute !important; left: -10000px !important; top: -1000px !important;` |
| `aHR0cHM6Ly9hZC5kb3VibGVjbGljay5uZXQvZmF2aWNvbi5pY28=` | `https://ad.doubleclick.net/favicon.ico` |
| `Q29udGVudC1UeXBl` | `Content-Type` |
| `aW1hZ2UveC1pY29u` | `image/x-icon` |
| `RGV2aWNlSUQ=` | `DeviceID` |

### Detection flow

1. Create bait `<div>` with ad-like class names, styled 1x1px off-screen
2. Wait 100ms
3. Check `offsetHeight === 0` — if the ad blocker's cosmetic filter collapsed it, adblock detected
4. Fallback: `fetch("https://ad.doubleclick.net/favicon.ico")` — if blocked or wrong Content-Type, adblock detected

### Result encoding

```js
const base = 10000 + 2 * Math.round(999 * Math.random());  // always even
$gsm.setCookie("DeviceID", adblockDetected ? base + 1 : base + 0, 7);
//                                           ODD=blocked  EVEN=clean
```

Cookie is re-set on every `beforeunload` event, 7-day TTL.

### Decoy

`window.suggestmeyes_loaded = true` — set on line 1 of misc.js. Red herring to confuse anti-detection scripts.

### EasyList hits on bait classes

| Class | EasyList rule |
|-------|---------------|
| `ad-unit` | `##.ad-unit` |
| `adv-mpu` | `##.adv-mpu` |
| `stickyads` | `##.stickyads` |
| `ads_banner` | `##.ads_banner` |
| `text-ad-top` | `##.text-ad-top` |

## System 2: srvb1.com Ad Reinsertion

**File:** `srvb1.com/o.js?uid=3de5c6e969bcbef63f8b7a59` (~2.8KB)

### Differential image bait

```js
// Both return 200 + image/gif server-side
// Only a filter rule can make one fail while the other loads
image1.src = "srvb1.com/px.gif?ch=1&rn=" + random;  // control
image2.src = "srvb1.com/px.gif?ch=2&rn=" + random;  // probe

// ch=1 ok + ch=2 fail = adblock
// both fail = network issue (NOT adblock — avoids false positives)
```

### On detection

Injects replacement banners from `srvb1.com/banner/<id>.jpg` into `.adv.banner-mpu > div` with a hand-built "Ad" label span.

### Current status

Already dead for EasyList users: `||srvb1.com^` (easylist.txt:51088) blocks the whole domain, which the script reads as "network failure, not adblock."

## System 3: Playwire RAMP + Blockthrough

**Loader:** `cdn.intergient.com/1025827/77706/ramp.js` (80KB)

Config includes `"blockthrough": true` — Blockthrough runs inside RAMP.

### Blockthrough probes

1. **URL bait:** `ad-delivery.net/px.gif?ch=2` (polled 2x, 400ms delay)
2. **URL bait:** `ad.doubleclick.net/favicon.ico?ad=300x250&...` (polled 2x, 400ms delay)
3. **DOM bait:** div with `ad_row adbannertop ad-mobile ad_sidebar adpopup boxad contentAd` (polled 4x, 200ms)
4. **Acceptable Ads:** `ad-delivery.net/px.gif?ch=1` (checks AA allowlist)
5. **DNS-level (Pi-hole/AdGuard):** `ab.dns-finder.com/meta/dns` + `wb.dns-finder.com/meta/dns`
6. **CSP violation listener:** catches blocked URIs
7. **Extension fingerprinting:** detects adblock/adblockplus extensions via iframe signal attributes

### Playwire PageOS bait

```js
const div = document.createElement("div");
div.classList.add("adLeaderboard", "adBanner", "leaderboard_ad");
// Check: getBoundingClientRect().height === 0

const img = new Image();
img.src = "https://raw.githubusercontent.com/easylist/easylist/master/docs/1x1.gif";
// EasyList's own repo, deliberately self-listed as bait
img.onerror = () => { adBlock = true; };
```

### Enforcement

`wall_mode: 0` (soft) — no hard paywall. Sets `ads_blocked` telemetry flag, skips interstitials, aborts Tyche loading. Recovery ads via `cdn.btmessage.com/script/rlink.js`. Results cached in localStorage `BT_AA_DETECTION`.

## System 4: Beacon Starvation

**Endpoint:** `counter-js.php3` (fired by `misc.js` on page/scroll)

EasyPrivacy line 741 (`/counter-js.php`) blocks this. The server computes a pages-to-beacons ratio per IP. Many pageviews + zero beacons = adblock or bot.

## Historical: Blockthrough standalone (2024, now replaced)

The 2024 homepage carried `btloader.com/tag` + a cloaked Prebid.js bundle on `dsh7ky7308k4b.cloudfront.net`. By September 2026, GSMArena switched to Playwire RAMP (which wraps Blockthrough internally). The standalone CloudFront delivery is gone.

## Other evasion tricks

| Trick | Purpose |
|-------|---------|
| `quicksearch-82795.jpg` serves JSON | Autocomplete data disguised as image |
| `cd836371f1d.cdn.intergient.com/fb87a4ea41` | Randomized telemetry hostname |
| Lambda fallback ingest URL | `yvpc7wicrtenmge7hkrswepja40vdkwt.lambda-url.us-east-1.on.aws` |
| `adlDisabled: ["desktop","tablet","mobile"]` | Recovery module switched off (edge flag) |
