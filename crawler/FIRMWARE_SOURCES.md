# Firmware / ROM download sources

Sites that publish downloadable stock firmware ("flash files" / ROMs) per device, by
brand family. **✓ = good candidate to add as a crawler parser** (regular per-model
listing pages). Verify each site's Terms before large crawls; some gate downloads
behind login, ads, waits, or paid tiers.

## Xiaomi / Redmi / POCO  (what we already crawl)
| Site | Notes |
|------|-------|
| **mifirm.net** ✓ *(implemented)* | 900+ models, fastboot + recovery, per region (China/Global/EEA/Russian/Indo/Taiwan/Japan/Turkey) |
| xiaomifirmwareupdater.com / xmfirmwareupdater.com | Community "XFU", very structured, has an API/JSON feed |
| xiaomirom.com | Fastboot & recovery, multi-region |
| miuirom.org | Stable recovery/fastboot/OTA |
| miuidownloader.com, mifirmware.com, hyperosupdate.com | HyperOS/MIUI mirrors |

## Samsung
| Site | Notes |
|------|-------|
| **samfw.com** ✅ *(IMPLEMENTED via browser-ingest)* | Cloudflare-gated, but crawled through a real browser (see [tools/browser_ingest.md](tools/browser_ingest.md)). Per **CSC/region** granularity (80+ regions/model); joins to gsmarena Models. `samfw.py` parser + `/ingest` endpoint |
| **firmwarefile.com** ✓ *(IMPLEMENTED — plain HTTP)* | `--ff samsung`; keyed by model code (SM-…), one firmware per model, joins to gsmarena's Models field |
| sammobile.com/firmwares | The classic archive; free tier is throttled |
| samfrew.com / samfw / galaxyfirmware.com / sfirmware.com | Mirrors, search by model/region/carrier |
| **Tools:** Frija, SamFirm(AiO), Bifrost | Pull straight from Samsung's own servers by model+CSC (not a website to scrape — a protocol client) |

## Tecno / Infinix / itel  (Transsion family — you asked about this)
| Site | Notes |
|------|-------|
| **romprovider.com** ✅ *(IMPLEMENTED — the working Tecno source)* | `--rp tecno` (also infinix/itel). Plain HTTP, no Cloudflare. Real Tecno stock ROMs (Camon, Spark, Pop, Pova, MegaPad) with **direct download links** (Google Drive / MediaFire .zip) |
| **firmwarefile.com** ✓ *(IMPLEMENTED for Infinix + itel)* | `--ff infinix`, `--ff itel`. NOTE: **no Tecno category** |
| **givemerom.com** ⚠️ *(partial — Cloudflare Turnstile)* | Sometimes auto-clears in a real browser (a Realme bucket was ingested that way), but often presents an interactive "Verify you are human" checkbox — a CAPTCHA we do **not** solve. Not a reliable source. Its Realme sample is NOT Tecno |
| needrom.com | Large community ROM repo (login required for many files) |
| hovatek.com forum | Transsion flashing know-how + files |
| **Tool:** Transsion "Test Tool" / TFT | After-sales flashing app for Tecno/Infinix/itel |

## Multi-brand / universal (Realme, Oppo, Vivo, OnePlus, Motorola, Nokia…)
| Site | Notes |
|------|-------|
| **firmwarefile.com** ✓ | Widest brand coverage; consistent per-model page layout |
| androidmtk.com | Stock ROM index across many brands |
| stockrom.net, gsmmafia.com, easy-firmware.com | Flash-file repositories |
| oppostockrom.com | Oppo/Realme specific |
| gsmserver.org / gsmedge | Realme/Oppo/Vivo + flash tools |
| getdroidtips.com, droidthunder.com | Guides + curated firmware links |

## Recommended next parsers to add (best structure ÷ effort)
1. **samfw.com** — covers your Samsung specs corpus; clean model → region/CSC → build pages.
2. **firmwarefile.com** — one parser unlocks Tecno + Infinix + itel + Realme + Oppo + more.
3. **xiaomifirmwareupdater** — a second, differently-structured Xiaomi source to cross-check mifirm.

Each is a `parse_*()` returning the same shape as `mifirm.parse_model()`, routed by
hostname in `crawler.scrape_device()`, then it flows through `export.py` and the app
automatically.

_Sources: samfw.com, sammobile.com, samfrew.com, firmwarefile.com, givemerom.com,
needrom.com, xiaomifirmwareupdater.com, xiaomirom.com, miuirom.org, androidmtk.com,
oppostockrom.com, getdroidtips.com, droidthunder.com._
