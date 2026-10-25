# MCP Price Scanner — public spec

A self-hosted MCP marketplace-fetching bridge for AI agents, running on a home
server (Proxmox LXC container). It exists for one reason: an agent running on
a cloud VM gets blocked by marketplace bot defenses (DataDome, Cloudflare,
login walls) because of its datacenter IP and sandbox limits. A small bridge
on a residential home connection, running a real browser, gets through far
more often — and can use Python scraping libraries the agent sandbox cannot.

This project is intended to be **public**. Everything in it must be
publishable: no personal data, no operator-specific details, no secrets.
See "Public-project rules" below — they are hard requirements, not guidance.

## What it does

- Exposes a small MCP server (FastMCP) with read-only marketplace tools.
- The agent calls the tools; the bridge fetches pages with a real browser
  (Playwright, persistent browser profile) from the home connection and
  returns structured listings.
- The bridge never buys, bids, messages sellers, or performs any account
  action. It is a reader.

## What it does not do

- No purchases, offers, bids, messages, or follows. No checkout flows.
- No bypassing of an explicit block. If a site returns a block page, a
  CAPTCHA wall, or a 403/429, the tool reports `blocked` with the reason and
  stops. No aggressive retries, no CAPTCHA solving, no fingerprint spoofing
  beyond running a normal, up-to-date browser.
- No notifications, no price-history database, no alerting. The agent that
  calls the tools owns all of that. v2 adds a persistent *response cache and
  cooldown store* (see "Persistent store (v2)"); it is not a price history:
  it keeps only the latest response per target, so the bridge can avoid
  repeating live requests.

## Architecture

- One LXC container on Proxmox (Debian stable). Suggested size: 2 vCPU,
  4 GB RAM, 16 GB disk. Nothing in the repo depends on a specific host, node
  name, container ID, or IP address.
- Python 3.12+, FastMCP server, Playwright with Chromium. `curl_cffi` may be
  used for plain HTTP fetches where a site does not need JS rendering.
- Transport: MCP over HTTP (streamable HTTP), served on localhost and
  exposed to the agent via the operator's own private network (for example a
  WireGuard/Tailscale-style tailnet, or a reverse proxy). The repo ships no
  network configuration with real hostnames, IPs, or tailnet names — only
  placeholders.
- Runs as a systemd service, non-root user, restarted on failure. Logs to
  stdout/journald only.

## MCP tools (v1)

All tools take plain arguments (site, query, limits) and return JSON. The
operator's own watch lists, criteria, and alert rules are **never** part of
this project — they live on the agent side and arrive as tool arguments.

- `search_marketplace(site, query, limit=20, sort="newest")`
  Search one configured source for a query string. Returns listings newest
  first where the source supports ordering.
- `fetch_listing(url)`
  Fetch one listing page and return its structured fields.
- `list_sources()`
  Return the configured sources and their capabilities (search yes/no,
  dates reliable yes/no, login required yes/no).
- `health()`
  Return server status and per-source reachability (reachable / blocked /
  login-required), without fetching listings.

### Listing schema (returned by search and fetch)

```json
{
  "id": "source-native listing id or URL slug",
  "site": "source key from config",
  "title": "listing title, verbatim",
  "price": 1234.56,
  "currency": "PLN",
  "location": "city or branch, as shown",
  "url": "canonical listing URL",
  "posted_date": "ISO date if the page shows one, else null",
  "date_reliable": true,
  "condition": "as shown, else null",
  "snippet": "short description excerpt, else null",
  "fetched_at": "ISO timestamp of this fetch"
}
```

- Never invent a field. If the page does not show a date, `posted_date` is
  null and `date_reliable` is false. The agent decides what "new" means.
- Prices are numbers as shown, plus the currency code as shown. No
  conversion, no fees, no shipping estimates in v1.

## Sources are configuration, not code

Sources are defined in a YAML config file (`config/sources.yaml`), one entry
per source: key, display name, search URL pattern, listing URL pattern,
whether search results carry reliable dates, whether login is required, and
any site-specific selectors. Adding a source must not require a code change
for a plain HTML source; a site needing custom parsing gets a small, named
parser module, documented in the config.

Example sources to ship configured (all public marketplaces; the operator
enables only the ones they need):

- `olx` — peer-to-peer classifieds, generally reachable.
- `allegro` — large marketplace; frequently DataDome-blocked from cloud IPs,
  the primary motivation for this bridge.
- `allegro-lokalnie` — classifieds side; listing dates often unreliable.
- `vinted` — second-hand marketplace; dates and availability flags can be
  unreliable (fetch artifacts occur).
- `facebook-marketplace` — login-gated; optional, disabled by default.
- `loombard` — pawnshop retail chain; branch stock, retail-style pricing,
  listings sometimes explicitly marked as replicas (surface that flag, never
  hide it).

A source that is blocked at runtime is reported as blocked, not retried in a
loop. A source that needs login stays disabled unless the operator
explicitly enables it and provides a browser profile (see Keys).

## Fetching behavior (politeness rules)

- One site at a time per search call; no parallel hammering of one site.
- Delay between requests to the same site: base 2–5 seconds plus jitter.
  Configurable per source, never zero.
- Stop immediately on HTTP 429, 403, block page, or CAPTCHA wall. Back off
  that source for a configurable cooldown (default 1 hour) and report
  `blocked` to the caller.
- Identify with a normal desktop browser user agent (the Playwright default
  for the installed Chromium). No fake identities, no rotating user agents.
- Cache nothing personal. Short-lived caching of fetched pages (minutes) is
  allowed to avoid duplicate fetches within one agent run. Since v2 a
  persistent store (below) keeps the latest response per target and the
  cooldowns across restarts; it holds public listing data, timestamps and
  cooldown state only. It is not a crawl database.

## Persistent store (v2)

Overriding principle: as few individual live requests as possible, and the
sources are respected at all times. Every design choice that would increase
the number of live requests (a shorter window, a retry, a background refresh)
is decided the other way.

- The store is checked first, on every `search_marketplace` and
  `fetch_listing` call, before any live request. A live request is the last
  resort.
- A *target* is one source plus one normalised search query (trim, case-fold,
  collapse whitespace; qualified by the requested sort order, since another
  sort is another results page), or one source plus one listing URL
  normalised as the fetcher normalises URLs.
- Concurrent identical targets are coalesced into at most one live request.
  One caller request never fans out into many live requests.
- No prefetching, no speculative fetching, no automatic fetching of the
  listings a search returned, no background refresh, no scheduled re-scan,
  no cache warming.
- No retry on a block, no immediate retry on a rate limit, no rotation of
  user agents, IP addresses or browser profiles, no headful or disguised
  browsing to evade a challenge. A block means: stop, cool down, serve the
  stored response or the blocked status.
- The per-site lock, the 2–5 s delay and the 10-minute health probe cache
  from v1 stay as they are.

Storage: one SQLite file (Python standard library `sqlite3`, no new
dependency), operator-owned, mode 600, outside the repository, location
`PRICE_SCANNER_DB_PATH`. WAL journal mode, a single writer. Two tables,
created with `CREATE TABLE IF NOT EXISTS` at startup:

- `responses`: source, kind (`search` | `fetch`), target, the response JSON
  exactly as returned, `fetched_at` (UTC), the source's block state at that
  time. One row per target, overwritten on refresh.
- `cooldowns`: source, target (`*` = source-wide, the only kind v2 writes),
  reason, `blocked_until` (UTC), `retry_after_seconds` at write time.

Cooldown model: the v1 per-source block cooldown (one hour after a 403, 429,
challenge page or CAPTCHA, from the timing config) is unchanged and is now
persisted. Not-yet-expired cooldowns are loaded at startup and honoured
before any fetch; expired ones are pruned at startup and on every write.

Freshness window: after a successful live search or fetch the same target is
not fetched live again until its window expires (defaults 6 hours for
searches, 24 hours for listings; `PRICE_SCANNER_SEARCH_CACHE_TTL_SECONDS`,
`PRICE_SCANNER_FETCH_CACHE_TTL_SECONDS`). Inside the window the stored
response is served flagged `cached`.

Reply contract (backward compatible; every v1 field is kept): `status` is
`ok` (fresh live result), `cached` (stored, inside the window), `cooldown`
(stored, served while the source is blocked) or `blocked` (blocked, nothing
stored); plus `from_cache`, `cached_at`, `age_seconds`, `next_refresh_at`,
`cooldown_until`, `cooldown_remaining_seconds`, `cooldown_reason` and
`target`. `health` reports the store's path (never its contents), whether it
is reachable, and the number of stored responses and active cooldowns.

Failure behaviour: if the database cannot be opened at startup, search and
fetch are refused with an error and the reason is logged; there is no silent
fall-back to in-memory-only operation. `list_sources` and `health(probe=false)`
keep working and `health` reports the store as down. A single failed write is
logged and counted, the service does not crash, and the affected target is
treated as uncached for that call only; after three consecutive failed writes
new live requests are refused until a write succeeds again.

## Public-project rules (hard requirements)

The repository must be publishable as-is. A reviewer cloning it must find
nothing belonging to any individual operator.

- **No personal data anywhere**: no names, no personal hostnames, IPs,
  tailnet names, addresses, phone numbers, emails, account names, or
  personal watch lists/criteria. Use placeholders (`<your-host>`,
  `<your-tailnet>`) in docs and examples.
- **No operator-specific configuration**: no real node names, container
  IDs, or paths from a real homelab. Example configs use generic values.
- **No personal accounts**: the project never assumes or references any
  operator's marketplace, social, or email account.

## Keys and secrets (hard requirements)

- **No secret ever enters the repository**: no tokens, passwords, cookies,
  session files, browser-profile directories, API keys, or private keys —
  not in code, not in config, not in docs, not in tests, not in git history.
- All secrets come from environment variables, read at startup:
  - `PRICE_SCANNER_TOKEN` — bearer token the agent presents to the MCP
    server. Generated by the operator at install time (e.g. `openssl rand
    -hex 32`), stored only in the operator's own environment/systemd
    override file, never in the repo.
  - `PRICE_SCANNER_PROFILE_DIR` (optional) — path to a Playwright
    persistent browser profile, outside the repo, used only for
    explicitly-enabled login-gated sources.
- Ship `.env.example` containing **placeholder names only** (e.g.
  `PRICE_SCANNER_TOKEN=change-me-at-install`), and a `.gitignore` covering
  `.env`, `*.session`, `profile/`, `auth/`, any local config override, and
  the persistent store (`*.db`, its WAL/journal files, `data/`).
- The persistent store never holds a secret or anything personal: no token,
  no caller identity, no browser state. Public listing data, timestamps and
  cooldown state only.
- Token comparison must be constant-time. A missing or wrong token returns
  401 with no detail.
- Logs must never print tokens, cookies, `Authorization`/`Set-Cookie`
  headers, or profile paths. Redact before logging.
- If a login-gated source is enabled, the operator creates the browser
  profile interactively on the bridge host and is advised to use a
  dedicated account for automated reads, never a personal main account.
  The project stores no credentials itself.
- Before any public release, run a secret scan over the full history
  (e.g. `gitleaks` or `git log -S` for token-like strings) and confirm a
  clean result. Record the check in the release notes, not the secrets.

## Installation (operator side, documented in README)

1. Create the LXC container, install Python and Playwright + Chromium.
2. Clone the repo, copy `.env.example` to `.env`, generate a token, fill
   in only the operator's own values.
3. Enable the sources needed in `config/sources.yaml`.
4. Start the systemd service, confirm `health()` passes.
5. Expose the MCP endpoint to the agent over the operator's private
   network, with the bearer token. Verify from the agent side with a single
   `health()` call before any search.

## Acceptance (what "done" means for v1)

- [ ] MCP server runs in a fresh container from the public repo alone, with
      only `.env` and `config/sources.yaml` supplied by the operator.
- [ ] `search_marketplace` returns structured listings for at least three
      configured sources; a blocked source reports `blocked`, not fake data.
- [ ] `fetch_listing` returns the listing schema above, with
      `posted_date: null, date_reliable: false` when the page shows no date.
- [ ] `list_sources` and `health` work without fetching listings.
- [ ] No secret, personal name, hostname, IP, or account reference exists
      anywhere in the repo (verified by secret scan + grep for placeholder
      violations).
- [ ] README documents install, configuration, tools, politeness rules,
      and the read-only boundary, using placeholders only.
- [ ] LICENSE file present (operator chooses the license at release).
- [ ] No buying, contacting, bidding, or account-action code exists in v1.

## Acceptance (v2: persistent store)

- [ ] A second identical search or fetch inside the freshness window returns
      the stored response flagged `cached`, with `cached_at` and
      `next_refresh_at`, and makes no live request.
- [ ] Concurrent identical requests make at most one live request; no
      prefetching, no automatic per-listing fetching after a search, no retry,
      rotation or evasion after a block; proven by a test that counts live
      requests.
- [ ] A target in a blocked cooldown returns the latest stored response
      flagged `cooldown` with `cooldown_until`, `cooldown_remaining_seconds`
      and `cooldown_reason` and makes no live request; with nothing stored it
      returns `blocked` with `retry_after_seconds` and no listings.
- [ ] Cooldowns and stored responses survive a service restart.
- [ ] If the database cannot be opened, search and fetch fail closed while
      `list_sources` and `health` keep working.

## Out of scope for v1 (do not build)

Buying, bidding, messaging sellers, login automation beyond a manually
created browser profile, price history/alerting, multi-user auth, a web UI,
or any operator-specific watch list or criteria engine.
