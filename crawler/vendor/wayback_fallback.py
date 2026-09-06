# wayback_fallback.py — vendored from gsmarena-unblock (root gsmarena_fetch.py), by arena.
# Last-resort spec fetch when the live origin is hard IP-banned (429). Reads
# archive.org ONLY — no evasion, no circumvention. Stdlib only.
import gzip, json
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

_WB_CDX = "http://web.archive.org/cdx/search/cdx"
_UA = "Mozilla/5.0 (compatible; device-crawler/1.0; +wayback-fallback)"


def wayback_fallback(url, timeout=45):
    """Return the newest non-poisoned archive.org snapshot of url, or None."""
    cdx = (_WB_CDX + "?url=" + quote_plus(url)
           + "&output=json&filter=statuscode:200&limit=-12&collapse=digest")
    try:
        rows = json.loads(urlopen(Request(cdx, headers={"User-Agent": _UA}),
                                  timeout=timeout).read().decode())
    except Exception:
        return None
    for row in reversed(rows[1:]):                 # rows[0] header; walk newest->oldest
        ts = row[1]
        snap = "https://web.archive.org/web/" + ts + "id_/" + url
        try:
            data = urlopen(Request(snap, headers={"User-Agent": _UA}), timeout=timeout).read()
        except Exception:
            continue
        if data[:2] == b"\x1f\x8b":                # raw id_ endpoint replays gzip bytes
            data = gzip.decompress(data)
        html = data.decode("utf-8", errors="replace")
        if "Too Many Requests" in html or len(html) < 2000:   # skip 429-poisoned captures
            continue
        return html
    return None
