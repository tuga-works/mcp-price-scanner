# MCP Price Scanner

A small, self-hosted [MCP](https://modelcontextprotocol.io) server that lets an
AI agent **read** public marketplace listings.

Agents running on cloud VMs are routinely blocked by marketplace bot defenses
(DataDome, Cloudflare, login walls) because of their datacenter IP addresses
and sandbox limits. This bridge runs on your own home server, on a residential
connection, fetches pages with a real browser (Playwright + Chromium) and
returns structured listings over MCP. The agent calls the tools; the bridge
fetches and parses.

The full design is in [SPEC.md](SPEC.md).

## Read-only boundary

The bridge is a reader. It never buys, bids, makes offers, messages sellers,
follows listings, or performs any account action, and the code contains no
such feature. It does not get around blocks either: a 403, 429, challenge
page or CAPTCHA wall is reported as `blocked` and the source is left alone
for a cooldown period. No CAPTCHA solving, no user-agent rotation, no
fingerprint tricks beyond a normal, current Chromium.

Your watch lists, criteria and alert rules are not part of this project. They
live on the agent side and arrive as plain tool arguments.

## Tools

All tools return JSON. Every response carries a `status`: `ok` (fresh live
result), `cached` (stored result inside its freshness window), `cooldown`
(stored result served while the source is blocked), `blocked` (source blocked,
nothing stored), `login-required`, `disabled` or `error`. See
[Persistent store, freshness and cooldowns](#persistent-store-freshness-and-cooldowns).

| Tool | What it does |
|---|---|
| `search_marketplace(site, query, limit=20, sort="newest")` | Searches one configured source (first results page only) and returns listings. `sort` is `newest`, `relevance`, `price_asc` or `price_desc`, applied where the source supports it (`sort_applied` says whether it was). Served from the persistent store when the same search was made inside its freshness window or while the source is cooling down; a live request is made only otherwise. |
| `fetch_listing(url)` | Fetches one listing page from a configured source and returns its fields. URLs of unconfigured sites are refused without a request. Served from the store inside its freshness window or during a cooldown. Only the URL the caller asked for is fetched; a search never fetches the listings it found. |
| `list_sources()` | Lists the configured sources and their capabilities: search yes/no, dates reliable yes/no, login required yes/no. Sends no requests. |
| `health(probe=true)` | Server status, the persistent store's state (path, reachable, number of stored responses and active cooldowns; never the contents) and per-source reachability: `reachable`, `blocked`, `login-required`, `disabled`, `unreachable` or `unknown`. With `probe=true` it sends at most one light request to each enabled source's home page (results are reused for 10 minutes, blocked sources are not contacted). With `probe=false` it reports the last known state and sends nothing. It never fetches listings. |

### Persistent store, freshness and cooldowns

The bridge makes as few live requests as possible and respects a source's
"no". A small SQLite file (Python's standard `sqlite3`, no extra dependency)
holds the latest response per *target* and the block cooldowns, and both
survive restarts. On every search and fetch the store is checked **first**;
a live request is the last resort.

A **target** is one source plus one normalised query for `search_marketplace`,
or one source plus one listing URL for `fetch_listing`:

- Query normalisation: trim, collapse every run of whitespace to one space,
  case-fold (Python `str.casefold()`). `"  Red   BIKE "` and `"red bike"` are
  one target. Nothing else is changed. Because a different sort order is a
  different results page, the stored target is `<sort>:<normalised query>`,
  e.g. `newest:red bike`; the response reports it as `target`.
- Listing URLs are normalised the way the fetcher already does (scheme
  `https`, host lowercased without a trailing dot, fragment and credentials
  dropped); the rebuilt URL is the target.

What happens on a call:

| Situation | Reply | Live request |
|---|---|---|
| Stored response younger than the freshness window | the stored response, `status: cached` | none |
| Source in its block cooldown, a response is stored | the latest stored response, `status: cooldown` | none |
| Source in its block cooldown, nothing stored | `status: blocked`, no listings | none |
| Otherwise | one live request, `status: ok`, stored | one (shared by concurrent identical callers) |

Freshness windows (defaults: searches 6 hours, listings 24 hours) are set with
`PRICE_SCANNER_SEARCH_CACHE_TTL_SECONDS` and `PRICE_SCANNER_FETCH_CACHE_TTL_SECONDS`.
The whole results page (up to the source's `max_results`) is stored, so a later
call with a larger `limit` is served from the store too. The block cooldown is
unchanged from v1 (one hour after a 403, 429, challenge page or CAPTCHA wall,
per source, `cooldown_seconds` in the source config); it is now written to the
store when it starts and restored at startup, so a restart cannot lift it.
There is no retry, no prefetching, no background refresh and no automatic
fetch of the listings a search returned.

Fields added to every search and fetch response that involves a target (all
v1 fields are unchanged):

| Field | Meaning |
|---|---|
| `status` | `ok`, `cached`, `cooldown` or `blocked` (plus the v1 `login-required`, `disabled`, `error`) |
| `from_cache` | `true` whenever the listings came from the store (or the short in-memory page cache), `false` for a live fetch |
| `cached_at` | UTC time the stored response was fetched; `null` for a live result |
| `age_seconds` | seconds since `cached_at`; `0` for a live result |
| `next_refresh_at` | earliest UTC time a live request for this target can happen again: `cached_at` plus the freshness window, or the end of the cooldown, whichever is later; `null` on errors |
| `cooldown_until` | UTC end of the source's block cooldown, or `null` |
| `cooldown_remaining_seconds` | seconds left in the cooldown, `0` when none |
| `cooldown_reason` | `http_403`, `http_429`, `challenge_page` ..., or `null` |
| `target` | the normalised target that was looked up |
| `retry_after_seconds` | (v1) kept on `blocked` and `cooldown` replies: equal to `cooldown_remaining_seconds` |

A `cached` reply, fresh enough, next live check at `next_refresh_at`:

```json
{"site": "olx", "query": "Gramofon", "sort": "newest", "target": "newest:gramofon",
 "status": "cached", "from_cache": true,
 "cached_at": "2026-10-25T11:40:12+00:00", "age_seconds": 905,
 "next_refresh_at": "2026-10-25T17:40:12+00:00",
 "cooldown_until": null, "cooldown_remaining_seconds": 0, "cooldown_reason": null,
 "search_url": "https://www.example-market.test/search?q=gramofon", "sort_applied": true,
 "dates_reliable": true, "fetched_at": "2026-10-25T11:40:12+00:00",
 "listings": ["... as stored ..."], "count": 5}
```

A `cooldown` reply, the source is blocked until `cooldown_until`, the latest
stored listings are returned instead of a new request:

```json
{"site": "allegro", "query": "road bike", "sort": "newest", "target": "newest:road bike",
 "status": "cooldown", "from_cache": true,
 "cached_at": "2026-10-25T08:02:41+00:00", "age_seconds": 13051,
 "cooldown_until": "2026-10-25T12:35:10+00:00", "cooldown_remaining_seconds": 2938,
 "cooldown_reason": "http_403", "retry_after_seconds": 2938,
 "next_refresh_at": "2026-10-25T12:35:10+00:00",
 "search_url": "https://www.example-market.test/search?q=road+bike", "sort_applied": true,
 "dates_reliable": false, "fetched_at": "2026-10-25T08:02:41+00:00",
 "listings": ["... the latest stored listings ..."], "count": 12}
```

A `blocked` reply, the source is blocked and nothing is stored for the target;
it contains no listings:

```json
{"site": "allegro", "query": "road bike", "sort": "newest", "target": "newest:road bike",
 "status": "blocked", "reason": "http_403", "retry_after_seconds": 3600,
 "from_cache": false, "cached_at": null, "age_seconds": 0,
 "cooldown_until": "2026-10-25T12:35:10+00:00", "cooldown_remaining_seconds": 3600,
 "cooldown_reason": "http_403", "next_refresh_at": "2026-10-25T12:35:10+00:00",
 "search_url": "https://www.example-market.test/search?q=road+bike", "listings": [], "count": 0}
```

`health` reports the store without its contents, and `freshness_seconds`:

```json
{"status": "ok", "version": "0.2.0", "freshness_seconds": {"search": 21600, "fetch": 86400},
 "store": {"path": "/opt/price-scanner/data/price-scanner.db", "reachable": true, "journal_mode": "wal",
           "responses": 42, "active_cooldowns": 1, "write_failures": 0,
           "consecutive_write_failures": 0, "degraded": false, "last_error": null},
 "sources": ["..."]}
```

**The database file.** One SQLite file at `PRICE_SCANNER_DB_PATH`, created at
startup with mode `600` (its parent directory is created with mode `700` when
missing); WAL journal mode, a single writer. Keep it outside the repository
(the shipped systemd unit uses `/opt/price-scanner/data/price-scanner.db`);
`*.db*` and `data/` are git-ignored in case it is ever created inside. It
holds only public listing data the tools already returned, UTC timestamps and
cooldown state: no caller identity, no token, no history of who asked what.
Stored responses are kept (one row per target, overwritten on refresh) so the
latest one can be served during a cooldown; nothing prunes them yet.

**Fail closed.** If the database cannot be opened at startup, `search_marketplace`
and `fetch_listing` are refused with `status: error` and the reason is logged;
there is no silent fall-back to unlimited in-memory fetching. `list_sources`
and `health(probe=false)` keep working, and `health` reports
`"status": "degraded"` with `store.reachable: false`. If a single write fails
mid-run it is logged and counted (`store.write_failures`), the live result is
still returned with a `store_warning`, and that target is simply fetched again
next time. After three consecutive failed writes the store is `degraded`:
stored responses are still served, but new live requests are refused until a
write succeeds again (each refused call re-checks writability).

### Listing schema

```json
{
  "id": "source-native listing id or URL slug",
  "site": "olx",
  "title": "listing title, verbatim",
  "price": 1234.56,
  "currency": "PLN",
  "location": "city or branch, as shown",
  "url": "https://www.example-market.test/item/123",
  "posted_date": "2026-10-24T10:16:00+02:00",
  "date_reliable": true,
  "condition": "as shown, else null",
  "snippet": "short description excerpt, else null",
  "fetched_at": "2026-10-24T20:50:00+00:00",
  "flags": []
}
```

- Nothing is invented. A field the page does not show is `null`.
- `posted_date` is an ISO date, or an ISO date-time with offset when the page
  shows a time. If the page shows no date, `posted_date` is `null` and
  `date_reliable` is `false`. Dates shown as an age ("2 days ago") and dates
  from sources whose dates are known to be unreliable are returned with
  `date_reliable: false`. A "refreshed on" date is not a posting date and is
  returned as `null`.
- `price` is the number shown and `currency` the currency shown. No
  conversion, no fees, no shipping estimates.
- `flags` lists markings the listing itself shows, e.g. `"replica"`. They are
  surfaced, never hidden. (This field is an addition to the schema in the
  spec.)

## Shipped sources

Configured in [`config/sources.example.yaml`](config/sources.example.yaml).
Enable only the ones you need.

| Key | Notes |
|---|---|
| `olx` | Peer-to-peer classifieds. Card and listing dates are posting dates, except cards marked "refreshed". |
| `allegro` | Large marketplace behind DataDome. Frequently reports `blocked`, even from a residential connection with a headless browser. Selectors are best effort. No posting dates are shown. |
| `allegro-lokalnie` | Classifieds side of Allegro. No dates are shown. |
| `vinted` | Second-hand marketplace. Item price without the buyer fee. Listing pages show only a relative upload age (`date_reliable: false`). |
| `facebook-marketplace` | Login-gated, **disabled by default**. See "Login-gated sources". |
| `loombard` | Pawnshop retail chain: branch stock at retail-style prices; location is the branch. Replica markings appear in `flags`. |

## Install

Target: a small Debian-stable LXC container (or any Linux host). Suggested
size 2 vCPU, 4 GB RAM, 16 GB disk. Python 3.12 or newer.

```sh
# 1. System packages and a service user (as root)
apt-get update && apt-get install -y python3 python3-venv git curl openssl
useradd --system --home-dir /opt/price-scanner --create-home --shell /usr/sbin/nologin price-scanner

# 2. Code and virtualenv (as the service user)
sudo -u price-scanner -H bash -c '
  cd /opt/price-scanner &&
  git clone https://github.com/tuga-works/mcp-price-scanner.git &&
  python3 -m venv venv &&
  venv/bin/pip install ./mcp-price-scanner &&
  venv/bin/playwright install chromium'

# 3. Chromium system libraries (as root)
/opt/price-scanner/venv/bin/playwright install-deps chromium

# 4. Directory for the persistent store, outside the repository (as root).
#    The service creates the database file itself with mode 600.
install -d -m 700 -o price-scanner -g price-scanner /opt/price-scanner/data
```

`sudo` is used for brevity; `runuser -u price-scanner -- ...` works the same
where sudo is not installed.

## Configure

Only two files are yours, and git ignores both: `.env` and
`config/sources.yaml`.

```sh
cd /opt/price-scanner/mcp-price-scanner
# The token: generated straight into the file, never echoed.
umask 077
{ grep -v '^PRICE_SCANNER_TOKEN=' .env.example
  printf 'PRICE_SCANNER_TOKEN=%s\n' "$(openssl rand -hex 32)"; } > .env
chown price-scanner:price-scanner .env

# Sources: copy the example and enable what you need.
install -m 640 -o price-scanner -g price-scanner config/sources.example.yaml config/sources.yaml
```

Environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `PRICE_SCANNER_TOKEN` | (required) | Bearer token the agent presents. At least 32 characters. The server refuses to start with the placeholder. |
| `PRICE_SCANNER_PROFILE_DIR` | unset | Playwright persistent profile for explicitly enabled login-gated sources. Must live outside the repository. |
| `PRICE_SCANNER_HOST` | `127.0.0.1` | Bind address. Localhost by default. |
| `PRICE_SCANNER_PORT` | `8765` | Port. The MCP endpoint is `http://<host>:<port>/mcp`. |
| `PRICE_SCANNER_CONFIG` | `config/sources.yaml` | Source config. If missing, the shipped example is used and a warning is logged. |
| `PRICE_SCANNER_HEADLESS` | `true` | Run Chromium headless. |
| `PRICE_SCANNER_LOG_LEVEL` | `INFO` | Log level. |
| `PRICE_SCANNER_CACHE_TTL_SECONDS` | `300` | In-memory page cache lifetime (a fast path in front of the fetcher). `0` disables it. |
| `PRICE_SCANNER_DB_PATH` | `data/price-scanner.db` | The SQLite file for stored responses and cooldowns (mode 600, created at startup). Put it outside the repository; the shipped unit sets `/opt/price-scanner/data/price-scanner.db`. If it cannot be opened the server fails closed (search and fetch refused). |
| `PRICE_SCANNER_SEARCH_CACHE_TTL_SECONDS` | `21600` (6 h) | Freshness window for search targets: the same source + normalised query is not fetched live again inside it. |
| `PRICE_SCANNER_FETCH_CACHE_TTL_SECONDS` | `86400` (24 h) | Freshness window for listing targets (source + listing URL). |
| `PRICE_SCANNER_ALLOWED_HOSTS` | unset | Extra `Host` header values to accept when a reverse proxy forwards requests, comma separated, e.g. `<your-host>.<your-tailnet>.ts.net`. |

### Adding a source

A plain HTML source needs no code: add an entry to `config/sources.yaml` with
a search URL pattern, the CSS selector of one result card, and a selector per
field. The header of `config/sources.example.yaml` documents every option.
A minimal example:

```yaml
sources:
  example-shop:
    name: Example shop
    enabled: true
    base_url: https://shop.example/
    default_currency: EUR
    search:
      url: https://shop.example/search?q={query_plus}
      sort: {newest: sort=new, price_asc: sort=price}
      item: '.product'
      fields:
        title: 'h2'
        url: {selector: 'a', attr: href}
        price: '.price'
        location: {selector: '.meta', label: 'Location:'}
```

A site that needs custom parsing gets a small named module in
`price_scanner/parsers/` and `parser: <name>` in its config entry
(`facebook-marketplace` is the example).

## Run

```sh
sudo cp systemd/price-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now price-scanner
journalctl -u price-scanner -f
```

The unit runs as the non-root `price-scanner` user, restarts on failure and
logs to the journal. For a manual foreground run: `venv/bin/price-scanner`
from the repository directory (it reads `.env` there).

### Check it

From the bridge host, with the token taken from `.env` without printing it:

```sh
cd /opt/price-scanner/mcp-price-scanner
TOKEN=$(sed -n 's/^PRICE_SCANNER_TOKEN=//p' .env)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp   # 401: no token
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}'
```

Or with the Python client that ships with FastMCP:

```python
import asyncio, os
from fastmcp import Client

async def main():
    async with Client("http://127.0.0.1:8765/mcp", auth=os.environ["TOKEN"]) as c:
        print((await c.call_tool("health", {"probe": True})).data)

asyncio.run(main())
```

### Expose it to your agent

The server listens on localhost. Expose it over your own private network, for
example a WireGuard/Tailscale-style tailnet or a reverse proxy, never directly
to the internet. Then point the agent's MCP client at
`http://<your-host>.<your-tailnet>:<port>/mcp` (or your proxy's URL) with the
header `Authorization: Bearer <your-token>`, and make one `health()` call
before any search. If a proxy forwards a different `Host` header, add it to
`PRICE_SCANNER_ALLOWED_HOSTS`.

## Politeness rules

- One request at a time per site, across all tool calls.
- A random delay of 2–5 seconds (configurable per source, never below 1 s)
  between two requests to the same site.
- On HTTP 429 or 403, a challenge page or a CAPTCHA wall: stop, report
  `blocked`, and send nothing to that source for the cooldown (default one
  hour). Calls during the cooldown return `blocked` with
  `retry_after_seconds` immediately.
- One search call fetches one results page. No crawling, no pagination loops.
- Chromium's default user agent; no fake identities. Images, media and fonts
  are not downloaded, which saves the sites bandwidth. The optional plain-HTTP
  mode (`fetch_mode: http`) uses curl_cffi presenting as a current Chrome.
- As few live requests as possible: the persistent store is checked before
  any request; a target is fetched live at most once per freshness window
  (6 h searches, 24 h listings by default); concurrent identical calls share
  one request; nothing is prefetched or refreshed in the background, and a
  search never fetches the listings it found. The short in-memory page cache
  (minutes) stays as a fast path.
- A block is final for the cooldown: no retry, no rotating user agents, IP
  addresses or browser profiles, no headful or disguised browsing to get past
  a challenge. The caller gets the latest stored response and the cooldown
  status instead of fresh data.
- On disk, only public listing data already returned, timestamps and cooldown
  state (see the database file above). Nothing personal is stored.

## Login-gated sources

Login-gated sources (`facebook-marketplace`) stay disabled unless you enable
them. To enable one:

1. Create a dedicated account for automated reads. Never use a personal main
   account.
2. On the bridge host, create a Playwright persistent profile outside the
   repository and log in to it by hand (this needs a display, e.g. a
   temporary desktop session or X forwarding):
   `venv/bin/python -c "from playwright.sync_api import sync_playwright as p; b=p().start().chromium.launch_persistent_context('<profile-dir>', headless=False); b.pages[0].goto('https://www.facebook.com/'); input('Log in, then press Enter'); b.close()"`
3. Set `PRICE_SCANNER_PROFILE_DIR=<profile-dir>` in `.env`, set
   `enabled: true` for the source in `config/sources.yaml`, restart.

The project stores no credentials. The profile directory is yours, outside the
repository, and listed in `.gitignore` patterns in case it is ever created
inside it.

## Keys and secrets

- No secret belongs in this repository: no tokens, passwords, cookies,
  session files, browser profiles, API keys or private keys.
- Secrets come only from the environment (`PRICE_SCANNER_TOKEN`,
  `PRICE_SCANNER_PROFILE_DIR`). `.env.example` holds placeholders only.
- Token comparison is constant-time. A missing or wrong token gets a 401 with
  an empty body.
- Logs pass through a redaction filter that removes the token, the profile
  path and any `Authorization`, `Cookie` or `Set-Cookie` values.
- The persistent store holds no secrets and nothing personal: only public
  listing data the tools already returned, UTC timestamps and cooldown state.
  The file and its WAL/journal are mode 600 and git-ignored.

## SSRF protection

`fetch_listing` takes URLs from the caller, and the bridge sits on a home
network, so it must never become a way to reach other machines:

- A listing URL is accepted only in a plain form: `http(s)` scheme, a host
  name (no IP literals, including numeric forms such as `2130706433`),
  default port, and no backslash, `@`, credentials, whitespace or control
  characters. The URL is rebuilt from its parsed parts (fragment dropped,
  `http` upgraded to `https`) and only the rebuilt string is fetched. Its
  host must equal, or be a subdomain of, a configured source's domain.
- All browser and HTTP traffic, including redirect hops and page
  subresources, leaves through a built-in egress guard: a loopback-only
  CONNECT proxy that tunnels TLS on port 443 to public addresses only. It
  resolves each host itself, refuses loopback, private, link-local,
  carrier-grade NAT/overlay, multicast and reserved addresses, and connects
  to the address it checked. TLS stays end to end, so the browser's identity
  is unchanged.
- Document navigations (pages and frames) outside the source's domains are
  aborted, and a page whose final URL left the source's domains is discarded
  without being parsed or cached.

For defence in depth, also restrict the container's outbound traffic to the
internet at the network level if your setup allows it.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/playwright install chromium
.venv/bin/pytest
```

The tests use synthetic HTML fixtures and temporary database files only and
send no network requests. `tests/test_store.py` counts live requests with a
mock fetcher: a cached call makes zero, concurrent identical calls make at
most one, a blocked call makes zero and is not retried.

## Release checklist

Before any public release, scan the full history for secrets and record the
result (not the secrets) in the release notes:

```sh
gitleaks git --redact -v .
```

## Limitations

- First results page only; `limit` caps at the source's `max_results`.
- Stored responses are never pruned (one row per target, overwritten on
  refresh); the file grows with the number of distinct targets.
- A source-wide cooldown: one block pauses every target of that source for
  the cooldown, as in v1.
- Site markup changes break selectors; an `ok` result with `count: 0` and a
  `note` usually means the layout changed.
- Allegro selectors could not be verified while the site was blocking.
- No price history, alerting, notifications or web UI. The calling agent owns
  all of that.

## License

Not chosen yet; see [LICENSE](LICENSE).
