"""Dhan REST v2 implementation of the normalized trade interface.

Lifted from the battle-tested surface the BTST strategy and the platform's
Dhan adapter already use — same endpoints, same ``dhanClientId`` payload
injection, same error parsing (2xx → success; anything else carries the
broker's ``errorCode``/``errorType``/``errorMessage``). Fill prices read
from ``averageTradedPrice`` (the ORDER BOOK field — ``tradedPrice`` exists
only in the trade-book endpoints).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests

from .base import (BrokerClient, BrokerError, OrderRequest, OrderResult,
                   ORDER_TYPE_LIMIT)

BASE_URL = "https://api.dhan.co/v2"


class DhanClient(BrokerClient):
    name = "dhan"

    def __init__(self, client_id: str, access_token: str,
                 base_url: str = BASE_URL, transport: Any = None):
        if not client_id or not access_token:
            raise BrokerError(
                "Dhan credentials missing — the caller (runtime config or "
                "the copy follow) must supply client_id + access_token")
        self.client_id = client_id
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        # transport: injectable for offline tests; the real one is requests.
        self._transport = transport or self._requests_transport

    # --- transport -----------------------------------------------------------
    @staticmethod
    def _requests_transport(method: str, url: str, headers: Dict[str, str],
                            body: Optional[dict]) -> Any:
        """The default transport: one requests call, returning the raw
        response. Tests inject a fake here — no network."""
        return requests.request(method, url, headers=headers, json=body,
                                timeout=20)

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """One Dhan REST call, parsed exactly like DhanHQ's own
        ``_parse_response``: 2xx → ``{"status": "success", "data": …}``;
        anything else → ``{"status": "failure", "remarks": {errorCode,
        errorType, errorMessage}}``. Raises nothing — callers decide what a
        failure means (the trade interface surfaces it loudly)."""
        headers = {"access-token": self.access_token,
                   "client-id": self.client_id,
                   "Content-Type": "application/json",
                   "Accept": "application/json"}
        if body is not None:
            # The SDK injects dhanClientId into every POST/PUT payload.
            body = {**body, "dhanClientId": self.client_id}
        url = f"{self.base_url}{path}"
        try:
            resp = self._transport(method.upper(), url, headers, body)
            payload = resp.json()
            status_code = resp.status_code
        except json.JSONDecodeError:
            return {"status": "failure",
                    "remarks": {"error_message": "non-JSON response"},
                    "data": {}, "_http": None}
        except Exception as exc:  # noqa: BLE001 — surfaced, not raised
            return {"status": "failure",
                    "remarks": {"error_message": f"{type(exc).__name__}: {exc}"},
                    "data": {}, "_http": None}
        if 200 <= status_code <= 299:
            # Dhan's body carries its own envelope ({"status", "remarks",
            # "data": …}) — unwrap it so callers read res["data"] as the
            # broker's data, one level, no double wrapping.
            if isinstance(payload, dict) and "data" in payload:
                return {"status": "success",
                        "remarks": payload.get("remarks") or {},
                        "data": payload.get("data"),
                        "_http": status_code}
            return {"status": "success", "remarks": {}, "data": payload,
                    "_http": status_code}
        remarks: dict = {}
        if isinstance(payload, dict):
            remarks = {"error_code": payload.get("errorCode"),
                       "error_type": payload.get("errorType"),
                       "error_message": payload.get("errorMessage")}
        return {"status": "failure", "remarks": remarks, "data": {},
                "_http": status_code}

    @staticmethod
    def _failed(res: dict) -> str:
        remarks = res.get("remarks") or {}
        return (f"Dhan call failed (HTTP {res.get('_http')}): "
                f"{remarks.get('error_code')} {remarks.get('error_type')} "
                f"{remarks.get('error_message')}").strip()

    # --- the trade surface ---------------------------------------------------
    def place_order(self, req: OrderRequest) -> OrderResult:
        body = {
            "transactionType": req.side,
            "exchangeSegment": req.exchange_segment,
            "productType": req.product,
            "orderType": req.order_type,
            "securityId": str(req.instrument_id),
            "quantity": int(req.quantity),
            "price": float(req.price) if req.order_type == ORDER_TYPE_LIMIT else 0.0,
            "disclosedQuantity": 0,
            "validity": req.validity,
            "afterMarketOrder": False,
            "amoFlag": "NO",
        }
        res = self._req("POST", "/orders", body)
        if res.get("status") != "success":
            raise BrokerError(self._failed(res))
        data = res.get("data") or {}
        order_id = str(data.get("orderId", ""))
        if not order_id:
            raise BrokerError(f"Dhan accepted the order but returned no id: {data}")
        return OrderResult(order_id=order_id,
                           status=str(data.get("orderStatus", "PENDING")),
                           raw=data)

    def modify_order_to_market(self, order_id: str, quantity: int) -> OrderResult:
        body = {"orderId": str(order_id), "orderType": "MARKET",
                "legName": "", "quantity": int(quantity), "price": 0.0,
                "triggerPrice": 0.0, "disclosedQuantity": 0,
                "validity": "DAY"}
        res = self._req("PUT", f"/orders/{order_id}", body)
        if res.get("status") != "success":
            raise BrokerError(self._failed(res))
        data = res.get("data") or {}
        return OrderResult(order_id=str(order_id),
                           status=str(data.get("orderStatus", "PENDING")),
                           raw=data)

    def cancel_order(self, order_id: str) -> bool:
        res = self._req("DELETE", f"/orders/{order_id}")
        if res.get("status") != "success":
            raise BrokerError(self._failed(res))
        return True

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        res = self._req("GET", "/orders")
        if res.get("status") != "success":
            raise BrokerError(self._failed(res))
        data = res.get("data") or []
        if not isinstance(data, list):
            return None
        for order in data:
            if str(order.get("orderId")) == str(order_id):
                return order
        return None

    def available_balance(self) -> float:
        """Available cash — LOUD on failure (never a silent zero: a funds
        call that fails must abort the trade, not pretend the account is
        empty). Dhan's own key spelling is ``availabelBalance`` (sic)."""
        res = self._req("GET", "/fundlimit")
        if res.get("status") != "success":
            raise BrokerError(self._failed(res))
        data = res.get("data") or {}
        if not isinstance(data, dict):
            raise BrokerError(f"unexpected fundlimit shape: {data!r}")
        return float(data.get("availabelBalance",
                              data.get("availableBalance",
                                       data.get("cashAmount", 0.0))) or 0.0)

    def get_positions(self) -> List[Dict[str, Any]]:
        res = self._req("GET", "/positions")
        if res.get("status") != "success":
            raise BrokerError(self._failed(res))
        data = res.get("data") or []
        return data if isinstance(data, list) else []

    # --- normalized accessors over raw records -------------------------------
    def order_result_of(self, order: Dict[str, Any]) -> OrderResult:
        """A raw order-book record → the normalized result (fill price from
        ``averageTradedPrice``, qty from ``filledQty``)."""
        return OrderResult(
            order_id=str(order.get("orderId", "")),
            status=str(order.get("orderStatus", "")),
            traded_price=self.fill_price_of(order),
            filled_qty=self.filled_qty_of(order),
            raw=order,
        )
