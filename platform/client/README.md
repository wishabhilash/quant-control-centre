# quant-client

The strategy-side client for the platform's market-data gateway, installed into
every strategy venv by the deploy (not published to PyPI — it is
platform-internal and coupled to the feed worker's protocol, so it ships inside
the deploy artifact).

```python
from quant_client import TickFeed

feed = TickFeed(config)               # config = load_config() from main.py
feed.ensure(["NSE:SBIN-EQ"])          # add-only subscribe (idempotent)
for tick in feed.stream(max_age=10):  # staleness-enforced iterator
    tick["instrument"]["symbol"], tick["ltp"]
```

- `ensure()` publishes a subscribe request on the account's Redis *control*
  channel and waits for the feed worker's ack, which carries the last tick per
  subscribed symbol (a strategy joining mid-session has current state
  immediately). Raising on failure — never silently pretending ticks flow.
  Symbols accepted here are TRACKED by the instance.
- `unsubscribe(symbols)` is its twin: the worker releases this feed's claim,
  and a symbol drops from the durable registry AND the broker socket only
  when its LAST claim goes. Claims are per CONSUMER — the dashboard claims
  under `dashboard`, a strategy under its `strategy_id` (auto-derived from
  the runtime config; override with `TickFeed(config, consumer="STR-009")`)
  — so two strategies sharing a symbol keep it alive for each other until
  both let go. The dashboard's tick page shows provenance per chip and its ✕
  removes only the dashboard's claim.
- `release_mine()` — the end-of-run sweep: releases EVERY claim held by
  THIS consumer (its `strategy_id`) across the account — including claims
  it can no longer name (aborted entries, stale runs). Strictly
  per-consumer: the dashboard's and other strategies' claims are never
  touched (the registry decides; tests prove the isolation).
- `stream(max_age=N)` yields ticks; a tick older than `N` seconds, or a silent
  gap longer than the staleness window, raises `TickStaleError` — the
  "never trade on a dead feed" rule is enforced here, once, not per strategy.
- Reconnect with backoff on Redis blips lives here too.
- **The context manager pairs every subscription with its teardown**:

  ```python
  with TickFeed(config) as feed:
      feed.ensure(["NSE:SBIN-EQ"])
      for tick in feed.stream(max_age=10):
          ...
  # ← exiting the context UNSUBSCRIBES what this instance ensure()d
  #   (best-effort — a failed drop prints a warning, never a masked error)
  ```

  A bare `feed.close()` does NOT unsubscribe anything — only the context
  manager or an explicit `unsubscribe()` prunes the account's desired set.

### Choosing the claim's lifetime (the one design decision per flow)

The context manager couples the claim to the **scope**; explicit
ensure/close couples it to whatever **you** decide. Match the claim to the
data's real lifetime:

- **Scope-coupled** — consume the feed inside the block, done with it after
  (a monitor watching one event, a one-shot price read):

  ```python
  with TickFeed(config) as feed:
      feed.ensure([symbol])
      for tick in feed.stream(max_age=10):
          handle(tick)
  # unsubscribed here, automatically
  ```

- **Position-coupled** — the claim must OUTLIVE the function (an entry
  pricing a symbol today whose monitor runs tomorrow morning): use
  `ensure()` + `close()` (no with-block — the context exit would unsubscribe
  at scope end and orphan the feed), and release explicitly when the
  position closes, or sweep with `release_mine()` at end of run.

The failure mode to avoid: refactoring a position-coupled `ensure` into a
with-block "for tidiness" — the scope exit silently unsubscribes a symbol
something else still needs.
