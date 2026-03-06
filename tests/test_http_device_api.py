# coding: utf-8

import base64
import io

import pytest
from PIL import Image

import uiautomator2 as u2
from uiautomator2._proto import HTTP_TIMEOUT
from uiautomator2.exceptions import HierarchyEmptyError


def test_httpdevice_click_supports_relative_coordinates(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")

    calls = []

    def fake_jsonrpc_call(method, params=None, timeout=10):
        calls.append((method, params, timeout))
        return True

    monkeypatch.setattr(d, "jsonrpc_call", fake_jsonrpc_call)
    monkeypatch.setattr(d, "window_size", lambda: (1000, 2000))

    d.click(0.5, 0.25)

    assert calls == [("click", (500, 500), HTTP_TIMEOUT)]


def test_httpdevice_swipe_converts_duration_to_steps(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")

    calls = []

    def fake_jsonrpc_call(method, params=None, timeout=10):
        calls.append((method, params, timeout))
        return True

    monkeypatch.setattr(d, "jsonrpc_call", fake_jsonrpc_call)
    monkeypatch.setattr(d, "window_size", lambda: (100, 200))

    d.swipe(0.1, 0.2, 0.9, 0.8, duration=0.1)

    assert calls == [("swipe", (10, 40, 90, 160, 20), HTTP_TIMEOUT)]


def test_httpdevice_dump_hierarchy_retry_empty(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    results = ["", "<hierarchy rotation=\"0\" />", "<hierarchy><node/></hierarchy>"]

    def fake_jsonrpc_call(_method, _params=None, _timeout=10):
        return results.pop(0)

    monkeypatch.setattr(d, "jsonrpc_call", fake_jsonrpc_call)

    assert d.dump_hierarchy() == "<hierarchy><node/></hierarchy>"


def test_httpdevice_dump_hierarchy_raises_when_empty(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")

    monkeypatch.setattr(d, "jsonrpc_call", lambda *_a, **_kw: "")

    with pytest.raises(HierarchyEmptyError):
        d._do_dump_hierarchy()


def test_httpdevice_screenshot_uses_jsonrpc(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")

    im = Image.new("RGB", (4, 3), color=(10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    def fake_jsonrpc_call(method, _params=None, _timeout=10):
        if method == "takeScreenshot":
            return b64
        raise AssertionError("unexpected method")

    monkeypatch.setattr(d, "jsonrpc_call", fake_jsonrpc_call)

    img = d.screenshot()
    assert isinstance(img, Image.Image)
    assert img.size == (4, 3)


def test_httpdevice_shell_via_jsonrpc(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    calls = []

    def fake_raw_jsonrpc_call(method, params=None, timeout=10):
        calls.append((method, params, timeout))
        return {"returnCode": 0, "stdout": "ok\n", "stderr": ""}

    monkeypatch.setattr(d, "_raw_jsonrpc_call", fake_raw_jsonrpc_call)
    resp = d.shell("echo test", timeout=3)

    assert resp.output == "ok\n"
    assert resp.exit_code == 0
    assert calls == [("executeShellCommand", ("echo test", 3000), HTTP_TIMEOUT)]


def test_httpdevice_app_list(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    monkeypatch.setattr(
        d,
        "shell",
        lambda *_a, **_kw: u2.ShellResponse("package:com.a\npackage:com.b\n", 0),
    )

    assert d.app_list() == ["com.a", "com.b"]


def test_httpdevice_app_stop_uses_force_stop(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    calls = []

    def fake_shell(cmdargs, timeout=60):
        calls.append((cmdargs, timeout))
        return u2.ShellResponse("", 0)

    monkeypatch.setattr(d, "shell", fake_shell)
    d.app_stop("com.demo")

    assert calls == [(["am", "force-stop", "com.demo"], 60)]


def test_httpdevice_app_start_with_monkey_and_stop(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    calls = []

    monkeypatch.setattr(d, "app_stop", lambda pkg: calls.append(("stop", pkg)))
    monkeypatch.setattr(d, "app_wait", lambda pkg, timeout=20.0, front=False: calls.append(("wait", pkg, timeout, front)) or 1234)

    def fake_shell(cmdargs, timeout=60):
        calls.append(("shell", cmdargs))
        return u2.ShellResponse("", 0)

    monkeypatch.setattr(d, "shell", fake_shell)
    d.app_start("com.demo", stop=True, wait=True)

    assert calls == [
        ("stop", "com.demo"),
        ("shell", ["monkey", "-p", "com.demo", "-c", "android.intent.category.LAUNCHER", "1"]),
        ("wait", "com.demo", 20.0, False),
    ]


def test_httpdevice_app_current(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    monkeypatch.setattr(
        d,
        "jsonrpc_call",
        lambda method, params=None, timeout=10: {"currentPackageName": "com.demo.app"}
        if method == "deviceInfo"
        else True,
    )
    monkeypatch.setattr(
        d,
        "shell",
        lambda *_a, **_kw: u2.ShellResponse(
            "mCurrentFocus=Window{42 u0 com.demo.app/.MainActivity}\n"
            "u0_a1 1318 123 0 0 0 0 0 S com.demo.app\n",
            0,
        ),
    )

    current = d.app_current()
    assert current["package"] == "com.demo.app"
    assert current["activity"] == ".MainActivity"


def test_httpdevice_app_wait_front(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    monkeypatch.setattr(d, "app_current", lambda: {"package": "com.demo.app", "activity": ".Main"})
    monkeypatch.setattr(
        d,
        "shell",
        lambda *_a, **_kw: u2.ShellResponse(
            "u0_a1 1318 123 0 0 0 0 0 S com.demo.app\n", 0
        ),
    )

    assert d.app_wait("com.demo.app", timeout=1.0, front=True) == 1318


def test_httpdevice_app_install_device_path(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    calls = []

    def fake_shell(cmdargs, timeout=60):
        calls.append(cmdargs)
        return u2.ShellResponse("Success\n", 0)

    monkeypatch.setattr(d, "shell", fake_shell)
    d.app_install("/data/local/tmp/app.apk")

    assert calls == [["pm", "install", "-r", "/data/local/tmp/app.apk"]]


def test_httpdevice_app_install_local_file_not_supported(tmp_path):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    apk = tmp_path / "a.apk"
    apk.write_bytes(b"fake-apk")

    with pytest.raises(u2.DeviceError):
        d.app_install(str(apk))


def test_httpdevice_session_checks_running(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    monkeypatch.setattr(d, "app_start", lambda package_name, activity=None, wait=False, stop=False, use_monkey=False: None)
    monkeypatch.setattr(d, "app_wait", lambda package_name, timeout=20.0, front=False: 1001)
    monkeypatch.setattr(d, "_pidof_app", lambda package_name: 1001)

    s = d.session("com.demo")
    monkeypatch.setattr(s, "_pidof_app", lambda package_name: 1001)
    monkeypatch.setattr(s, "jsonrpc_call", lambda method, params=None, timeout=10: {"ok": True})
    assert s.running() is True
    assert s.jsonrpc.deviceInfo() == {"ok": True}


def test_httpdevice_app_uninstall_all(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    monkeypatch.setattr(
        d,
        "shell",
        lambda cmdargs, timeout=60: u2.ShellResponse(
            "package:com.a\npackage:com.b\npackage:com.github.uiautomator\n", 0
        ),
    )
    calls = []
    monkeypatch.setattr(d, "app_uninstall", lambda pkg: calls.append(pkg) or True)

    pkgs = sorted(d.app_uninstall_all(excludes=["com.b"]))

    assert pkgs == ["com.a"]
    assert calls == ["com.a"]


def test_httpdevice_app_auto_grant_permissions(monkeypatch):
    d = u2.HTTPDevice("http://127.0.0.1:9008")
    calls = []

    def fake_shell(cmdargs, timeout=60):
        calls.append(cmdargs)
        if cmdargs == ["getprop", "ro.build.version.sdk"]:
            return u2.ShellResponse("33\n", 0)
        if cmdargs == ["dumpsys", "package", "com.demo"]:
            return u2.ShellResponse(
                "targetSdk=33\n"
                "android.permission.CAMERA: granted=false\n"
                "android.permission.RECORD_AUDIO: granted=true\n"
                "android.permission.ACCESS_FINE_LOCATION: granted=false\n",
                0,
            )
        return u2.ShellResponse("", 0)

    monkeypatch.setattr(d, "shell", fake_shell)
    d.app_auto_grant_permissions("com.demo")

    assert ["pm", "grant", "com.demo", "android.permission.CAMERA"] in calls
    assert ["pm", "grant", "com.demo", "android.permission.ACCESS_FINE_LOCATION"] in calls


def test_httpdevice_public_method_parity_with_device():
    def pub_methods(cls):
        out = set()
        for name in dir(cls):
            if name.startswith("_"):
                continue
            attr = getattr(cls, name, None)
            if callable(attr):
                out.add(name)
        return out

    missing = sorted(pub_methods(u2.Device) - pub_methods(u2.HTTPDevice))
    assert missing == []


def test_httpdevice_unsupported_transfer_and_service_ops():
    d = u2.HTTPDevice("http://127.0.0.1:9008")

    with pytest.raises(u2.DeviceError):
        d.push("a", "/data/local/tmp/a")
    with pytest.raises(u2.DeviceError):
        d.pull("/data/local/tmp/a", "a")
    with pytest.raises(u2.DeviceError):
        d.start_uiautomator()
    with pytest.raises(u2.DeviceError):
        d.stop_uiautomator()
    with pytest.raises(u2.DeviceError):
        d.reset_uiautomator()
