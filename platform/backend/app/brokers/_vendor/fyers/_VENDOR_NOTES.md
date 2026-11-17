# Vendored: Fyers market-data WebSocket client

Source: [`fyers-apiv3` 3.2.1](https://pypi.org/project/fyers-apiv3/)
(`fyers_apiv3/FyersWebsocket/{data_ws.py,defines.py,map.json}`),
MIT license — see `LICENSE.txt` (© 2019 FYERS).

**Why vendored instead of installed:** the PyPI package pins
`asyncio==3.4.3` (an obsolete placeholder package whose *name shadows the
stdlib `asyncio` module* on modern Python) and `requests==2.31.0` (an exact
pin that fights the platform's `requests>=2.31` requirement). The data
socket needs only `websocket-client` and `requests` at runtime, so those two
modules and the symbol maps are vendored; `websocket-client` and `redis` are
the only dependencies this adds to `requirements.txt`.

**Patches applied to the upstream files (keep minimal):**

1. `data_ws.py` imports:
   - `from fyers_apiv3.FyersWebsocket import defines` → `from . import defines`
   - `from fyers_apiv3.fyers_logger import FyersLogger` → `from . import logger`
     (`logger.py` re-implements the wrapper's call surface with stdlib logging —
     upstream imports `aws_lambda_powertools`)
   - `from pkg_resources import resource_filename` dropped; `map.json` is
     resolved next to this module (`MAP_JSON_PATH`)
2. **No log file.** Upstream writes a `fyersDataSocket.log` into the CWD
   (golden rule 4: never a second log sink); the vendored logger logs to the
   platform logger instead, so the `FyersDataSocket(log_path=...)` handler
   creation was removed.

Do not fix SDK bugs here beyond what the platform needs — prefer recording
them and re-vendoring from a newer upstream release.
