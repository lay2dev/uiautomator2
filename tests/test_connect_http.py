# coding: utf-8

from unittest.mock import Mock, patch

import pytest

import uiautomator2 as u2
from uiautomator2.exceptions import RPCUnknownError


def test_connect_http_url_returns_http_device():
    d = u2.connect("http://192.168.50.27:9008")
    assert isinstance(d, u2.HTTPDevice)
    assert d._rpc_endpoint == "http://192.168.50.27:9008/jsonrpc/0"


def test_connect_uses_env_rpc_url(monkeypatch):
    monkeypatch.setenv("UIAUTOMATOR2_RPC_URL", "http://192.168.50.27:9008")

    def _should_not_call_connect_usb(_serial=None):
        raise AssertionError("connect_usb should not be called when UIAUTOMATOR2_RPC_URL is set")

    monkeypatch.setattr(u2, "connect_usb", _should_not_call_connect_usb)

    d = u2.connect()
    assert isinstance(d, u2.HTTPDevice)


def test_httpdevice_jsonrpc_success():
    d = u2.HTTPDevice("http://192.168.50.27:9008")
    resp = Mock()
    resp.status_code = 200
    resp.reason = "OK"
    resp.text = '{"jsonrpc":"2.0","id":1,"result":{"sdkInt":36}}'
    resp.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": {"sdkInt": 36}}

    with patch("uiautomator2.requests.post", return_value=resp) as m:
        result = d.jsonrpc.deviceInfo(http_timeout=3)

    assert result == {"sdkInt": 36}
    m.assert_called_once_with(
        "http://192.168.50.27:9008/jsonrpc/0",
        json={"jsonrpc": "2.0", "id": 1, "method": "deviceInfo", "params": {}},
        headers={
            "User-Agent": "uiautomator2",
            "Accept-Encoding": "",
            "Content-Type": "application/json",
        },
        timeout=3,
    )


def test_httpdevice_jsonrpc_error():
    d = u2.HTTPDevice("http://192.168.50.27:9008")
    resp = Mock()
    resp.status_code = 200
    resp.reason = "OK"
    resp.text = '{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"boom","data":"stack"}}'
    resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32001, "message": "boom", "data": "stack"},
    }

    with patch("uiautomator2.requests.post", return_value=resp):
        with pytest.raises(RPCUnknownError):
            d.jsonrpc.deviceInfo()
