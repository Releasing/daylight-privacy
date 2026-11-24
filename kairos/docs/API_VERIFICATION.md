# Official API verification log (24 Nov 2026)

## What was attempted

| Source (spec p.20) | Direct fetch from build sandbox | Result |
|---|---|---|
| https://docs.kalshi.com/ | blocked (sandbox network policy, DNS/CONNECT 403) | verified via search-engine excerpts of official pages only |
| https://api.elections.kalshi.com/trade-api/v2 | blocked | not callable here |
| https://kalshi.com/fee-schedule | blocked | excerpts only |
| https://help.kalshi.com/en/articles/13823838 | blocked | excerpts only |
| Coinbase / Kraken WS docs | blocked | not verified — adapters schema-tolerant |
| Anthropic pricing | not needed (Claude disabled by default; rates must be entered by operator) | — |

**Consequence:** nothing below is treated as final. `kairos verify-api` (backend CLI) must be run on the
VPS; it calls the public endpoints, records response shapes to `data/fixtures/verified/`, and writes a
fingerprint the engine requires before leaving `OBSERVE` (paper) or ever enabling live mode.

## Facts corroborated from official-doc excerpts

- Order book endpoint returns `orderbook_fp` with `yes_dollars` / `no_dollars` arrays of
  `[price_dollars_string, count_string]`; **bids only**; YES ask = 1 − best NO bid. Legacy integer-cent
  `orderbook.yes/no` removed in 2026 (secondary sources). Prices up to 4 decimals; counts may be fractional
  strings ("4.04").
- WebSocket: `wss://api.elections.kalshi.com/trade-api/ws/v2` (demo `wss://demo-api.kalshi.co/trade-api/ws/v2`);
  authentication required at handshake with the same three headers as REST
  (`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`); signed path `/trade-api/ws/v2`, method `GET`.
  Subscribe: `{"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":[...]}}`;
  first message `orderbook_snapshot`, then `orderbook_delta` with `seq`; gap → resubscribe.
- Signing: RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length, base64; message =
  timestamp(ms) + METHOD + path **without query string**. (Official WS page text says seconds while its own
  example uses ms — the implementation uses ms, matching REST examples; `verify-api --auth` confirms.)
  Docs also mention Ed25519 keys as an option; RSA-PSS is implemented per spec p.4.
- Order creation: legacy `POST /portfolio/orders` slated for deprecation no earlier than 6 May 2026; V2 path
  `POST /portfolio/events/orders`; duplicate `client_order_id` → HTTP 409 `order_already_exists`
  (look up via Get Orders — never blind retry). `count` may be a fixed-point string.
- Fee types per series: `quadratic`, `quadratic_with_maker_fees`, `flat`; `fee_multiplier` float; general
  taker `ceil_to_cent(0.07·C·P·(1−P))`, maker 0.0175 where charged; **fee rounding uses a per-order
  accumulator across fills** (docs `getting_started/fee_rounding`). Event-level fee overrides exist.
- Candlesticks: `GET /series/{series_ticker}/markets/{ticker}/candlesticks` (`period_interval` in minutes).
- Hosts seen in docs: `api.elections.kalshi.com` and `external-api.kalshi.com` → base URL is configuration.
- BTC 15-minute series `KXBTC15M`, market ticker form `KXBTC15M-26NOV120030`. Settlement: CF Benchmarks
  BRTI; YES if the 60-second simple average before close is **at least** (≥) the 60-second average before
  open (Robinhood-mirrored rules text). Post-expiration index revisions not applied. Market rules text
  controls — the engine parses and hashes `rules_primary`/`rules_secondary` and blocks entries if the parsed
  rule template differs from the verified template.

## Not verifiable here (handled conservatively)

- Exact market field names for bid/ask (`yes_bid_dollars` vs `yes_bid`), strike fields (`floor_strike`,
  `functional_strike`), status enumerations → parser accepts known variants; unknown → market ineligible.
- Rate-limit tier numbers → configurable token buckets default to conservative 10 read/s, 5 write/s.
- Whether `orderbook_delta` WS channel is available to a read-only key → if not, REST polling fallback.
- Coinbase Advanced Trade public channels auth requirements; Kraken v2 checksum algorithm → Kraken book
  checksum validation is implemented as documented in v2 (CRC32 of top-10 price/qty strings) but marked
  unverified; on mismatch the venue is marked stale rather than trusted.

Sources (search excerpts): docs.kalshi.com (quick_start_market_data, get-multiple-market-orderbooks,
websocket_connection, api_keys, create-order, create-order-v2, fee_rounding, get-market-candlesticks,
series models), help.kalshi.com 13823838, kalshi.com/fee-schedule PDF, GitHub issues
peetah-138/nfl-combo-planner#70 and troyrhodes02/sightline#91 (secondary).
