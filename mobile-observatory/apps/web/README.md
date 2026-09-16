# Mobile Observatory Web

A dependency-free, local-first interface for four focused workflows:

- **Radar** — unseen firmware changes, Android upgrades, and security patches.
- **Explore** — supported-device and exact-silicon filtering.
- **Security** — evidence-backed advisory → component → chip → device relationships.
- **Admin** — source health and offline snapshot operations.

## Run locally

Serve the repository (ES modules do not run from `file://`):

```sh
python -m http.server 8080 --directory mobile-observatory/apps/web
```

Then open `http://localhost:8080`. The interface requests `/api/v1`. If the API is unavailable it enters a visibly labeled demonstration mode using `fixtures.js`; fixture data is never sent to the API or treated as canonical.

To use a different API root, define `window.OBSERVATORY_API_BASE` before `app.js` loads.

## API boundary

All network access lives in `api.js`. Views do not know database tables, collector formats, or storage details.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/radar/overview` | Counts, snapshot metadata, latest run |
| GET | `/api/v1/updates` | Firmware change-event feed |
| POST | `/api/v1/updates/{id}/acknowledge` | User-local acknowledgement |
| GET | `/api/v1/devices` | Canonical supported-device search |
| GET | `/api/v1/chips` | Canonical silicon search |
| GET | `/api/v1/security/findings` | Security relation findings |
| GET | `/api/v1/admin/health` | Collector/source health |
| GET | `/api/v1/search?q=` | Cross-entity search (reserved for suggestions) |

List responses may be JSON arrays or `{ "items": [...] }`. Query values are URL encoded and empty values omitted. Errors are represented by `ApiError`, never silently translated into domain state. The only exception is initial application startup, where an unavailable API activates the explicit demo dataset.

## Design constraints

- UI labels preserve uncertainty: **Open**, **Claimed fixed**, and **Unknown** are distinct.
- A marketing name, model code, and exact chip part remain visibly separate.
- Source freshness appears alongside results; an empty result is not presented as proof of absence.
- Desktop tables remain horizontally scrollable on narrow screens rather than dropping evidence fields.
- Keyboard `/` focuses global search; navigation and controls use native accessible elements.
