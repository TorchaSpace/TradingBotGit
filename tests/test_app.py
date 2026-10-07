"""Desktop app API (local server) - offline, with a fake exchange."""
import http.client
import json
import time
import zipfile

import numpy as np
import pytest

import tradingbot.app.engine as engine
import tradingbot.app.export as export
import tradingbot.app.server as server
import tradingbot.envfile as envfile
import tradingbot.live as live
from test_live_exchange import FakeSpot


@pytest.fixture
def app(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(envfile, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(envfile, "EXAMPLE_PATH", tmp_path / "missing.example")
    for mod in (engine, server, export, live):
        monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(engine, "SESSIONS_FILE", state / "sessions.json")
    monkeypatch.setattr(export, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(export, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(server, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(live, "STOP_FILE", tmp_path / "STOP")
    fx = FakeSpot(100 * np.exp(np.linspace(0, 0.5, 400)))
    monkeypatch.setattr(live, "make_exchange", lambda s, authenticated=None: fx)
    import tradingbot.report as report
    monkeypatch.setattr(report, "STATE_DIR", state)
    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "reports")
    a = server.App()
    httpd = server.serve(a)
    yield a
    a.engines.stop_all()
    httpd.shutdown()


def call(a, method, path, body=None, token=True, host="127.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", a.port, timeout=10)
    h = {"Host": f"{host}:{a.port}", "Content-Type": "application/json"}
    if token:
        h["X-Token"] = a.token
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    try:
        return r.status, json.loads(data)
    except json.JSONDecodeError:
        return r.status, data


def test_requires_token_and_local_host(app):
    assert call(app, "GET", "/api/bootstrap", token=False)[0] == 403
    assert call(app, "GET", "/api/bootstrap", host="evil.example")[0] == 403
    code, b = call(app, "GET", "/api/bootstrap")
    assert code == 200 and b["configured"] is False and b["fields"]
    assert call(app, "GET", "/", token=False)[0] == 403
    assert call(app, "GET", f"/?t={app.token}", token=False)[0] == 200


def test_settings_validation_and_secret_masking(app):
    code, b = call(app, "POST", "/api/settings", {"values": {"RISK_PER_TRADE": "0.5"}})
    assert code == 400                                    # 50% risk is rejected
    code, _ = call(app, "POST", "/api/settings", {"values": {
        "RISK_PER_TRADE": "0.004", "BINANCE_DEMO_API_KEY": "KEY_abcdefghijklmnop", "BINANCE_DEMO_API_SECRET": "SEC_zyxwvutsrqponm"}})
    assert code == 200
    b = call(app, "GET", "/api/bootstrap")[1]
    assert b["values"]["BINANCE_DEMO_API_KEY"] == {"set": True, "hint": "•••mnop"}
    assert "abcdefghijklmnop" not in json.dumps(b)        # the key never goes back to the UI
    call(app, "POST", "/api/settings", {"values": {"BINANCE_DEMO_API_KEY": "•••mnop"}})   # masked = unchanged
    assert envfile.read_env()["BINANCE_DEMO_API_KEY"] == "KEY_abcdefghijklmnop"
    assert envfile.read_env()["RISK_PER_TRADE"] == "0.004"


def test_live_switch_needs_typed_phrase_and_start_needs_ack(app):
    call(app, "POST", "/api/settings", {"values": {"BINANCE_LIVE_API_KEY": "LIVEKEY_123456789", "BINANCE_LIVE_API_SECRET": "LIVESEC_987654321"}})
    assert call(app, "POST", "/api/account/switch", {"mode": "live", "phrase": "evet"})[0] == 400
    assert call(app, "POST", "/api/account/switch", {"mode": "live", "phrase": "gerçek para"})[0] == 200
    assert envfile.read_env()["LIVE_TRADING_CONFIRM"] == "I_UNDERSTAND_THE_RISK"
    code, b = call(app, "POST", "/api/engine/start", {"kind": "trend"})
    assert code == 400 and "onay" in b["error"]
    assert call(app, "POST", "/api/account/switch", {"mode": "paper"})[0] == 200
    assert envfile.read_env()["LIVE_TRADING_CONFIRM"] == ""


def test_demo_switch_without_keys_is_refused(app):
    code, b = call(app, "POST", "/api/account/switch", {"mode": "demo"})
    assert code == 400 and "API" in b["error"]


def test_start_stop_paper_bot_and_export_has_no_secrets(app):
    call(app, "POST", "/api/settings", {"values": {"SYMBOLS": "BTC/USDT", "BTC_FILTER": "false",
                                                    "TELEGRAM_BOT_TOKEN": "123456:SECRET_TELEGRAM_TOKEN"}})
    code, b = call(app, "POST", "/api/engine/start", {"kind": "trend"})
    assert code == 200, b
    assert engine.wait_until(lambda: (app.engines.objects.get("trend") is not None and
                                      bool(app.engines.objects["trend"].trades)), 10)
    st = call(app, "GET", "/api/status")[1]
    assert st["engines"]["trend"]["running"] is True
    assert call(app, "POST", "/api/settings", {"values": {"RISK_PER_TRADE": "0.004"}})[0] == 409  # locked while running
    dash = call(app, "GET", "/api/dashboard")[1]
    assert "open" in dash, dash
    assert dash["open"] and dash["open"][0]["symbol"] == "BTC/USDT"
    logs = call(app, "GET", "/api/logs?after=0")[1]["records"]
    assert any("ENTER" in r["msg"] for r in logs)
    code, b = call(app, "POST", "/api/engine/stop", {"kind": "trend"})
    assert code == 200 and not app.engines.running("trend")
    code, b = call(app, "POST", "/api/export", {})
    assert code == 200
    with zipfile.ZipFile(b["path"]) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    assert "state/journal_paper_spot.csv" in names and "sessions.json" in [n.split("/")[-1] for n in names]
    assert b"SECRET_TELEGRAM_TOKEN" not in blob
    sessions = json.loads(open(engine.SESSIONS_FILE).read())
    assert sessions[-1]["ended"] and sessions[-1]["settings"]["TELEGRAM_BOT_TOKEN"].startswith("•••")


def test_validation_job_api_and_staleness(app, tmp_path, monkeypatch):
    import webbrowser
    import tradingbot.validation as V
    from test_validation import _candles
    monkeypatch.setattr(V, "OUT_DIR", tmp_path / "reports")
    monkeypatch.setattr(V, "load_for", lambda s, since="2020-01-01": (
        {sym: _candles(i, 0.0012 + 0.0003 * i) for i, sym in enumerate(s.symbols)}, _candles(0, 0.0012)))
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u))
    call(app, "POST", "/api/settings", {"values": {"SYMBOLS": "BTC/USDT,ETH/USDT"}})
    b = call(app, "GET", "/api/validation")[1]
    assert b["result"] is None and b["job"]["running"] is False
    assert call(app, "POST", "/api/validation/open", {})[0] == 404
    assert call(app, "POST", "/api/validation/run", {})[0] == 200
    assert call(app, "POST", "/api/validation/run", {})[0] == 409          # only one at a time
    assert engine.wait_until(lambda: not app.validation.running(), 120)
    b = call(app, "GET", "/api/validation")[1]
    assert b["job"]["error"] is None, b["job"]
    r = b["result"]
    assert r["verdict"] and r["mc_holdout"]["prob_loss"] is not None and len(r["risk_curve"]) == 6
    assert "fan" not in r["mc_all"] and b["stale"] is None and b["live"] == []
    assert (tmp_path / "reports" / "validation_spot.html").exists()
    assert call(app, "POST", "/api/validation/open", {})[0] == 200 and opened
    logs = [x["msg"] for x in call(app, "GET", "/api/logs?after=0")[1]["records"]]
    assert any("Doğrulama bitti" in m for m in logs)
    # changing the risk makes the saved result stale
    call(app, "POST", "/api/settings", {"values": {"RISK_PER_TRADE": "0.004"}})
    assert "risk_per_trade" in call(app, "GET", "/api/validation")[1]["stale"]
    # and the export zip carries the validation files
    code, e = call(app, "POST", "/api/export", {})
    with zipfile.ZipFile(e["path"]) as z:
        assert "validation/validation_spot.json" in z.namelist()
