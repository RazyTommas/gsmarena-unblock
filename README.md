# gsmarena-unblock

Reverse-engineered GSMArena's ad-block detection and built bypass tools.

GSMArena uses **three independent client-side detectors** that funnel into one server-side enforcement: an HTTP 429 wall with a 10-hour ban. There is no client-side overlay — the kill switch is a cookie.

## How the detection works

```
                          ┌─────────────────────┐
                          │    misc.js (1P)      │
                          │  bait div + dc fetch │
                          └─────────┬───────────┘
                                    │ parity-encoded
                                    ▼
┌──────────────┐    ┌───────────────────────────────┐    ┌──────────────┐
│  srvb1.com   │───▶│   DeviceID cookie             │◀───│  Playwire    │
│  diff pixels │    │   ODD = blocked, EVEN = clean │    │  RAMP + BT   │
└──────────────┘    └───────────────┬───────────────┘    └──────────────┘
                                    │
                    ┌───────────────┤
                    │               │
                    ▼               ▼
            ┌──────────────┐ ┌──────────────────┐
            │ beacon ratio │ │ IP reputation    │
            │ counter-js   │ │ request cadence  │
            └──────┬───────┘ └────────┬─────────┘
                   │                  │
                   ▼                  ▼
            ┌─────────────────────────────────┐
            │  HTTP 429 · Retry-After: 36000  │
            │         (10-hour ban)            │
            └─────────────────────────────────┘
```

### The DeviceID cookie trick

`misc.js` creates a bait `<div>` with ad-like class names, checks `offsetHeight === 0` after 100ms, then probes `ad.doubleclick.net/favicon.ico`. The result is encoded in the **parity** of a `DeviceID` cookie:

- **Odd** = ad-blocker detected
- **Even** = clean

The server reads `value % 2` on every request. The cookie is re-set on every `beforeunload`, so single-clear doesn't work.

## Quick start

### Browser users — uBlock Origin filter list

Import `filters/gsmarena-bypass.txt` into uBlock Origin:

1. Open uBlock Origin → Dashboard → Filter lists → Import
2. Paste: `https://raw.githubusercontent.com/<you>/gsmarena-unblock/main/filters/gsmarena-bypass.txt`
3. Apply changes

Or add the rules manually in "My filters":

```
! Force DeviceID cookie to even (neutralizes server signal)
gsmarena.com##+js(trusted-set-cookie, DeviceID, 10000)

! Allow counter beacon (prevents beacon starvation signal)
@@||gsmarena.com/counter-js.php3

! Allow doubleclick favicon probe (it's detection, not an ad)
@@||ad.doubleclick.net/favicon.ico$domain=gsmarena.com

! Allow ad-delivery probe (Blockthrough detection)
@@||ad-delivery.net/px.gif$domain=gsmarena.com
```

### Programmatic access — Python

```bash
pip install -r requirements.txt
python gsmarena.py search "Samsung Galaxy S24"
python gsmarena.py specs samsung_galaxy_s24-12644
python gsmarena.py brands
```

The client handles cookie management, detection evasion, and rate limiting automatically.

### Two separate walls — know which one you hit

GSMArena has **two independent blocking systems**. Bypassing them requires different things:

| Wall | Trigger | Fix |
|------|---------|-----|
| **Ad-block detection** | `DeviceID` cookie parity, blocked ad probes | The 4 filter rules / this client's cookie forcing |
| **IP/ASN 429 ban** | Datacenter IP, scraper-rate requests, bad IP reputation | A clean **residential** IP + human request rate |

The cookie bypass only helps on a **clean IP**. It cannot rescue an already-banned IP — the 429 is stamped server-side against the IP before any cookie is read. Datacenter and proxy IPs (including Tor and most reader-proxies) are 429-banned outright.

### Automatic Wayback fallback

When the live origin returns 429, this client automatically falls back to the **Wayback Machine**, which serves the same `data-spec` markup from archive.org's infrastructure — so `specs` works from **any** IP, banned or not:

```
$ python gsmarena.py specs samsung_galaxy_s23-12082
⚠ Live origin returned 429 (IP banned). Falling back to Wayback Machine...
  ✓ Wayback snapshot 20260826204014
{ "name": "Samsung Galaxy S23", "specs": { "chipset": "Snapdragon 8 Gen 2 ...", ... } }
```

The fallback walks newest→oldest and skips "poisoned" snapshots (captures where Wayback's own crawler hit the 429 wall).

### Already banned?

For **live** access: change your IP (VPN, residential proxy, router restart / mobile hotspot to cycle a dynamic IP) and apply the bypass rules before visiting again. The ban is IP-based with a 10-hour TTL. For **data** access regardless of ban: use the Wayback fallback (automatic in this client).

## Project structure

```
gsmarena-unblock/
├── README.md                    # this file
├── DETECTION.md                 # full technical analysis
├── filters/
│   └── gsmarena-bypass.txt      # uBlock Origin filter list
├── gsmarena.py                  # Python query client with bypass
├── requirements.txt             # Python dependencies
└── deobfuscated/
    ├── misc_js_detector.js      # annotated detection code from misc.js
    └── srvb1_detector.js        # annotated srvb1.com detection code
```

## Detection systems

| System | Source | Method | Signal |
|--------|--------|--------|--------|
| Native | `misc.js` | bait div + doubleclick fetch | `DeviceID` cookie parity |
| srvb1 | `srvb1.com/o.js` | differential pixel loads (ch=1 vs ch=2) | ad reinsertion |
| Playwire | `cdn.intergient.com/ramp.js` | multi-probe (DOM, network, DNS, CSP) | telemetry only |
| Beacon | `counter-js.php3` | EasyPrivacy blocks the beacon | pages-to-beacons ratio |

## How we found this

5-agent investigation using Claude Code, each on a different model and attack angle, coordinating findings through a shared mailbox system:

- **gsma-http** (Sonnet) — HTTP response analysis, misc.js deobfuscation
- **gsma-js** (Fable) — full JavaScript deobfuscation across all 4 systems
- **gsma-dom** (Haiku) — DOM bait elements, CSS traps, community issues
- **gsma-network** (Opus) — network flow mapping, RAMP/Blockthrough internals
- **gsma-bypass** (Opus) — community intel, comparative analysis, bypass validation

Agents cross-verified findings: gsma-js corrected gsma-http's initial analysis with misc.js evidence; gsma-network independently confirmed the DeviceID mechanism; gsma-bypass validated bypass approaches against community reports.

## License

MIT
