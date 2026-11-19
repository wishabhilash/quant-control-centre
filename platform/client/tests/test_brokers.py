"""The broker-trade interface, offline: transport injected, no network.

The FakeTransport records every wire call and replays scripted responses —
asserting the NORMALIZED → Dhan wire mapping, the loud error surfacing, and
the fill-price field handling.
"""
import pytest

from quant_client.brokers import (BrokerError, DhanClient, OrderRequest,
                                  ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET,
                                  PRODUCT_MARGIN, SIDE_BUY, SIDE_SELL)


class FakeTransport:
    """Scripted transport: one (method_matcher, path, response) rule list.

    Each call records (method, url, headers, body) and returns the first
    matching rule's (status, payload) — or 500 {} when nothing matches."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])   # (method, path_suffix, status, payload)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        for rule_method, suffix, status, payload in self.rules:
            if method == rule_method and url.endswith(suffix):
                class _R:
                    status_code = status

                    def json(self):
                        return payload
                return _R()
        class _R:
            status_code = 500

            def json(self):
                return {}
        return _R()


def client(transport) -> DhanClient:
    return DhanClient("CID-1", "tok-1", transport=transport)


def auth_headers(headers):
    return (headers.get("access-token") == "tok-1"
            and headers.get("client-id") == "CID-1")


def req() -> OrderRequest:
    return OrderRequest(symbol="NSE:MARUTI26NOVFUT", instrument_id="1333",
                        exchange_segment="NSE_FNO", side=SIDE_BUY,
                        quantity=100, order_type=ORDER_TYPE_LIMIT,
                        price=13541.0, product=PRODUCT_MARGIN)


# --- request validation -------------------------------------------------------
def test_limit_order_requires_a_price():
    with pytest.raises(BrokerError):
        OrderRequest(symbol="NSE:X", instrument_id="1",
                     exchange_segment="NSE_FNO", side=SIDE_BUY, quantity=1,
                     order_type=ORDER_TYPE_LIMIT, price=0.0)


def test_invalid_side_and_type_are_rejected():
    with pytest.raises(BrokerError):
        OrderRequest(symbol="NSE:X", instrument_id="1",
                     exchange_segment="NSE_FNO", side="SIDEWAYS", quantity=1)
    with pytest.raises(BrokerError):
        OrderRequest(symbol="NSE:X", instrument_id="1",
                     exchange_segment="NSE_FNO", side=SIDE_BUY, quantity=1,
                     order_type="ICEBERG")


# --- place order --------------------------------------------------------------
def test_place_order_maps_the_wire_body_and_parses():
    transport = FakeTransport([
        ("POST", "/orders", 200,
         {"orderId": "23226", "orderStatus": "TRANSIT"})])
    result = client(transport).place_order(req())

    assert result.order_id == "23226"
    assert result.status == "TRANSIT"
    call = transport.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/orders")
    assert auth_headers(call["headers"])
    body = call["body"]
    # The normalized intent → the Dhan wire shape…
    assert body["transactionType"] == "BUY"
    assert body["securityId"] == "1333"
    assert body["exchangeSegment"] == "NSE_FNO"
    assert body["orderType"] == "LIMIT" and body["price"] == 13541.0
    assert body["quantity"] == 100 and body["productType"] == "MARGIN"
    # …and the SDK's dhanClientId payload injection.
    assert body["dhanClientId"] == "CID-1"


def test_market_order_carries_zero_price():
    transport = FakeTransport([("POST", "/orders", 200, {"orderId": "1",
                                                         "orderStatus": "TRADED"})])
    r = req()
    r.order_type = ORDER_TYPE_MARKET
    client(transport).place_order(r)
    assert r and transport.calls[0]["body"]["price"] == 0.0


def test_place_order_surfaces_the_brokers_real_error():
    transport = FakeTransport([
        ("POST", "/orders", 401,
         {"errorType": "Invalid_Authentication", "errorCode": "DH-901",
          "errorMessage": "Client ID or access token is invalid"})])
    with pytest.raises(BrokerError) as excinfo:
        client(transport).place_order(req())
    assert "DH-901" in str(excinfo.value)
    assert "Invalid_Authentication" in str(excinfo.value)


# --- modify / cancel / order book ---------------------------------------------
def test_modify_to_market_uses_put_with_remaining_qty():
    transport = FakeTransport([("PUT", "/orders/9", 200,
                                {"orderId": "9", "orderStatus": "PENDING"})])
    client(transport).modify_order_to_market("9", quantity=50)
    call = transport.calls[0]
    assert call["method"] == "PUT" and call["url"].endswith("/orders/9")
    assert call["body"]["orderType"] == "MARKET"
    assert call["body"]["quantity"] == 50


def test_get_order_finds_by_id_and_maps_the_fill_price():
    book = [{"orderId": "A", "orderStatus": "TRADED",
             "averageTradedPrice": 7900.55, "filledQty": 100},
            {"orderId": "B", "orderStatus": "REJECTED"}]
    transport = FakeTransport([("GET", "/orders", 200, {"data": book})])
    c = client(transport)

    found = c.get_order("A")
    assert found is not None
    result = c.order_result_of(found)
    # The ORDER BOOK field is averageTradedPrice — the Rs 0.00 fill-price
    # regression came from reading the trade-book's field name here.
    assert result.traded_price == 7900.55
    assert result.filled_qty == 100
    assert result.status == "TRADED"

    assert c.get_order("ZZZ") is None


def test_available_balance_reads_the_dhan_typo_key():
    transport = FakeTransport([
        ("GET", "/fundlimit", 200,
         {"status": "success",
          "data": {"availabelBalance": 222874.75,   # Dhan's own spelling
                   "sodLimit": 1.0}})])
    assert client(transport).available_balance() == 222874.75


def test_available_balance_is_loud_on_failure():
    transport = FakeTransport([
        ("GET", "/fundlimit", 401,
         {"errorType": "Invalid_Authentication", "errorCode": "DH-901"})])
    with pytest.raises(BrokerError) as excinfo:
        client(transport).available_balance()
    assert "DH-901" in str(excinfo.value)


def test_positions_returns_the_list():
    transport = FakeTransport([("GET", "/positions", 200,
                                {"data": [{"securityId": "1333",
                                           "netQty": 100}]})])
    positions = client(transport).get_positions()
    assert positions[0]["netQty"] == 100
