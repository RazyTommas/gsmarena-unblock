#!/usr/bin/env python3
"""
probe_sources.py — what can THIS network actually see?

Run on the remote box. It answers the only question that matters before any real
collection is assigned: which of our sources answer here, and which of the answers
are real.

WHY STATUS CODES ARE NOT THE MEASUREMENT
Three of our sources return HTTP 200 for things that do not exist:
  * docs.qualcomm.com serves a byte-identical Angular shell for EVERY path, including
    paths that were never real. A 200 there carries zero information.
  * support.apple.com/en-us/100100/rss returns 200 with a "Page Not Found" body.
  * support.apple.com/rss/security.rss returns valid RSS, a current pubDate, and
    permanently zero <item>s.
And one returns 200 with a challenge page instead of content (gsmarena search).

So every probe below asserts on CONTENT — a sentinel string, a minimum size, a
non-zero record count — and the SPA case additionally fetches a deliberately bogus
sibling path: if the real path and the nonsense path come back identical, the host is
answering everything and the probe reports `shell`, never `ok`.

THE CONTROL
`example.com` is fetched first and last. If the control fails, nothing else in the
report is a finding about any source — it is instrument failure, and `control_ok`
goes to false so the asking side discards the numbers instead of reading them.

    python3 probe_sources.py --out <dir>
"""
from __future__ import annotations
import argparse, json, socket, ssl, sys, time, urllib.error, urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 30


def get(url, method="GET", data=None, headers=None, timeout=TIMEOUT):
    """Returns (status, body_bytes, err). Never raises — a probe that dies on the
    first blocked host tells you nothing about the other twenty."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, None
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
        return None, b"", f"{type(e).__name__}: {e}"


def check(name, url, *, sentinel=None, min_bytes=400, shell_probe=None,
          method="GET", data=None, headers=None, count_tag=None, note=""):
    """One source. `shell_probe` is a nonsense sibling URL: if it returns the same
    bytes as the real one, the host answers everything and the 200 means nothing."""
    t0 = time.time()
    st, body, err = get(url, method=method, data=data, headers=headers)
    ms = int((time.time() - t0) * 1000)
    out = {"source": name, "url": url, "status": st, "bytes": len(body),
           "ms": ms, "error": err, "note": note}
    if err:
        out["verdict"] = "unreachable"
        return out
    text = body.decode("utf-8", "replace")

    if shell_probe:
        st2, body2, _ = get(shell_probe)
        out["shell_probe_status"] = st2
        out["shell_probe_bytes"] = len(body2)
        if body2 and len(body2) == len(body) and body2 == body:
            out["verdict"] = "shell"
            out["detail"] = ("a deliberately bogus path returned BYTE-IDENTICAL content, "
                             "so status codes from this host carry no information")
            return out

    if st is None or st >= 400:
        out["verdict"] = "blocked" if st in (401, 403, 429) else "http-error"
        return out
    if count_tag:
        n = text.count(count_tag)
        out["records"] = n
        if n == 0:
            out["verdict"] = "empty-but-200"
            out["detail"] = f"200 with zero {count_tag!r} — a false green"
            return out
    if len(body) < min_bytes:
        out["verdict"] = "too-small"
        out["detail"] = f"{len(body)}B is below the {min_bytes}B floor for real content"
        return out
    if sentinel and sentinel.lower() not in text.lower():
        out["verdict"] = "wrong-content"
        out["detail"] = f"sentinel {sentinel!r} absent — served something, not the thing"
        return out
    out["verdict"] = "ok"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    a = ap.parse_args()
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)

    # ---- control, before anything else -------------------------------------
    c0 = check("CONTROL example.com", "https://example.com",
               sentinel="Example Domain", min_bytes=200,
               note="if this fails, no number below is a finding")
    control_ok = c0["verdict"] == "ok"
    print(f"CONTROL: {c0['verdict']} ({c0['status']}, {c0['bytes']}B)")
    if not control_ok:
        print("  ! control failed — this network cannot reach a known-good host.")

    checks = [c0]

    print("\n--- the blocked-here sources (the reason this box exists) ---")
    checks += [
        # Samsung patch levels: THE binding constraint. 39 of 1,207 devices have an
        # SPL and every one came from samfw. If this box can reach it, that is the
        # single most valuable result in this probe.
        check("samfw.com (device page)",
              "https://samfw.com/firmware/SM-A045F",
              sentinel="firmware", min_bytes=2000,
              note="Samsung firmware + security patch level"),
        check("fota-cloud (Samsung manifest)",
              "https://fota-cloud-dn.ospserver.net/firmware/ILO/SM-A045F/version.xml",
              sentinel="<versioninfo", min_bytes=200,
              note="the manifest samsung.py reads; blocked by WAF on the other box"),
        check("gsmarena brand page",
              "https://www.gsmarena.com/samsung-phones-9.php",
              sentinel="makers", min_bytes=8000,
              note="device slug index -> chipset coverage"),
        check("gsmarena device page",
              "https://www.gsmarena.com/samsung_galaxy_a06-13191.php",
              sentinel="Chipset", min_bytes=8000,
              note="the spec fields we enrich from"),
        check("gsmarena SEARCH (known-gated)",
              "https://www.gsmarena.com/res.php3?sSearch=galaxy+a06",
              sentinel="Chipset", min_bytes=8000,
              note="expected to fail even here — Turnstile. A PASS is real news."),
    ]

    print("\n--- Qualcomm: the SPA that answers everything ---")
    checks += [
        check("qualcomm GetCollection",
              "https://docs.qualcomm.com/bundle/publicresource/GetCollection/securitybulletin",
              sentinel="collectionId", min_bytes=20,
              shell_probe="https://docs.qualcomm.com/bundle/publicresource/GetCollection/"
                          "this-path-was-never-real-xyz",
              note="returns a collectionId on the other box"),
        check("qualcomm globalsearch (500s on the other box)",
              "https://docs.qualcomm.com/bundle/publicresource/globalsearch",
              method="POST", headers={"Content-Type": "text/plain"},
              data=json.dumps({
                  "start": 0, "rows": 5, "searchText": "", "isGroupBy": False,
                  "sortFields": [{"field": "score", "order": "desc"}],
                  "filterFields": [{"field": "pkDocumentPath", "values": ["PDC20903"]}],
                  "filterFieldsQuery": [], "IsFacetSearch": False, "IsCustomSearch": False,
                  "IsProductContext": False, "IsTranslation": False, "urlState": "",
                  "dcnEntitlement": False, "specState": ""}).encode(),
              sentinel="dcn", min_bytes=50,
              note="Content-Type must be EXACTLY text/plain; charset -> 415, json -> 500"),
    ]

    print("\n--- sources that work on the other box (regression controls) ---")
    checks += [
        check("OSV Android index", "https://api.osv.dev/v1/vulns/ASB-A-528209857",
              sentinel="ecosystem_specific", min_bytes=200),
        check("NVD 2.0", "https://services.nvd.nist.gov/rest/json/cves/2.0"
              "?cveId=CVE-2025-21479", sentinel="vulnerabilities", min_bytes=200),
        check("MediaTek bulletin",
              "https://www.mediatek.com/product-security-bulletin/september-2026",
              sentinel="CVE-", min_bytes=5000),
        check("Samsung SMR (whole year)",
              "https://security.samsungmobile.com/securityUpdate.smsb?year=2026",
              sentinel="SMR", min_bytes=10000),
        check("Xiaomi trust (the 19-month silence)",
              "https://trust.mi.com/bff/security-update-detail/synctime/202412",
              sentinel="{", min_bytes=2),
        check("Xiaomi EOS (liveness control)",
              "https://trust.mi.com/bff/eos-products/phones?type=Xiaomi-Redmi-POCO",
              sentinel="sku", min_bytes=500),
        check("Apple advisory index", "https://support.apple.com/en-us/100100",
              sentinel="Available for", min_bytes=10000),
        check("Apple RSS (known false green)",
              "https://support.apple.com/rss/security.rss",
              count_tag="<item", min_bytes=100,
              note="expected empty-but-200; a real item count here would be news"),
        check("AppleDB", "https://api.appledb.dev/ios/iOS;22H374.json",
              sentinel="build", min_bytes=100),
        check("CISA KEV",
              "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
              sentinel="vulnerabilities", min_bytes=10000),
        check("mifirm.net", "https://mifirm.net/", sentinel="firmware", min_bytes=2000),
    ]

    # control again at the end: a network that died halfway would otherwise leave the
    # later sources looking blocked when in fact nothing was reachable any more.
    c1 = check("CONTROL example.com (post)", "https://example.com",
               sentinel="Example Domain", min_bytes=200)
    checks.append(c1)
    control_ok = control_ok and c1["verdict"] == "ok"

    for c in checks:
        flag = {"ok": "  ok  ", "blocked": "BLOCKED", "shell": " SHELL",
                "unreachable": " UNREACH", "empty-but-200": " EMPTY",
                "wrong-content": " WRONG", "too-small": " SMALL",
                "http-error": " HTTP"}.get(c["verdict"], c["verdict"])
        print(f"  [{flag}] {c['source']:38} {str(c['status']):>5} "
              f"{c['bytes']:>8}B {c.get('detail','')[:60]}")

    (outdir / "probe.json").write_text(json.dumps(
        {"probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "control_ok": control_ok, "checks": checks}, indent=2))

    ok = [c for c in checks if c["verdict"] == "ok" and not c["source"].startswith("CONTROL")]
    bad = [c for c in checks if c["verdict"] not in ("ok",)
           and not c["source"].startswith("CONTROL")]
    # The headline the other box needs: which of the BLOCKED-THERE sources work HERE.
    unlocked = [c["source"] for c in ok if c["source"].split()[0] in
                ("samfw.com", "fota-cloud", "gsmarena", "qualcomm")]
    note = (f"{len(ok)} reachable, {len(bad)} not. "
            f"Unlocked by this IP: {unlocked or 'NONE'}")
    (outdir / "result.json").write_text(json.dumps(
        {"outcome": "ok" if control_ok else "error",
         "note": note if control_ok else "CONTROL FAILED — discard these numbers",
         "control_ok": control_ok,
         "counts": {"reachable": len(ok), "unreachable": len(bad),
                    "unlocked_here": len(unlocked)},
         "files": ["probe.json"]}, indent=2))
    print(f"\n{note}")
    return 0 if control_ok else 1


if __name__ == "__main__":
    sys.exit(main())
