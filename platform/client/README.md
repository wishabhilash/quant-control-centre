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
- `stream(max_age=N)` yields ticks; a tick older than `N` seconds, or a silent
  gap longer than the staleness window, raises `TickStaleError` — the
  "never trade on a dead feed" rule is enforced here, once, not per strategy.
- Reconnect with backoff on Redis blips lives here too.

v0 is deliberately **add-only**: strategies cannot unsubscribe (pruning stays
on the tick-data page in the dashboard). Removal-by-strategy needs refcounting
across consumers.
