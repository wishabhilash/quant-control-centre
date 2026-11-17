"""Vendored Fyers market-data WebSocket client (from fyers-apiv3 3.2.1).

Why vendored: ``pip install fyers-apiv3`` carries pins that are hostile to the
platform venv — notably ``asyncio==3.4.3`` (an ancient placeholder package
whose name shadows the stdlib module) and ``requests==2.31.0`` (an exact pin).
The **data socket** itself needs only ``websocket-client`` and ``requests`` at
runtime, so the two relevant modules (``data_ws.py``, ``defines.py``) plus the
symbol maps (``map.json``) are vendored under the MIT license (see LICENSE.txt,
© 2019 FYERS) with the minimal patches recorded in ``_VENDOR_NOTES.md``.

Keep changes to the vendored files to the bare minimum — upgrades are easier
when the diff against upstream stays small.
"""
