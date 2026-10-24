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
- No notifications, no price-history database, no alerting in v1. The agent
  that calls the tools owns all of that. (A storage extension is a later,
  separate decision.)

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
  allowed to avoid duplicate fetches within one agent run; no persistent
  crawl database in v1.

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
  `.env`, `*.session`, `profile/`, `auth/`, and any local config override.
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

## Out of scope for v1 (do not build)

Buying, bidding, messaging sellers, login automation beyond a manually
created browser profile, price history/alerting, multi-user auth, a web UI,
or any operator-specific watch list or criteria engine.
