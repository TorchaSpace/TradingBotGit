"""Desktop app entry point: `python -m tradingbot app` (or double-click TradingBot.app on a Mac).

Opens a native window (pywebview) when available, otherwise the default browser.
Closing the window stops the bots cleanly; open positions keep their stop orders on Binance.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
import webbrowser

from ..config import PROJECT_ROOT
from .server import App, serve

LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging(app: App) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        if h is not app.logbuf:
            root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root.addHandler(fh)
    if sys.stdout and sys.stdout.isatty():
        root.addHandler(logging.StreamHandler(sys.stdout))
    for noisy in ("ccxt", "urllib3", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _mac_branding() -> None:
    """Show 'TradingBot' + its icon in the Dock/menu bar instead of 'Python' (macOS, best effort)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage  # type: ignore  (pyobjc, installed with pywebview)
        from Foundation import NSBundle  # type: ignore
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "TradingBot"
        icon = PROJECT_ROOT / "TradingBot.app" / "Contents" / "Resources" / "AppIcon.icns"
        if icon.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initWithContentsOfFile_(str(icon)))
    except Exception:
        pass


def main(no_window: bool = False, port: int = 0) -> int:
    app = App()
    setup_logging(app)
    httpd = serve(app, port)
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    log = logging.getLogger("tradingbot.app")
    log.info("TradingBot uygulaması açıldı")
    try:
        if no_window:
            raise ImportError
        import webview  # pywebview
    except ImportError:
        print(f"TradingBot: {url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("TradingBot", url, width=1320, height=860, min_size=(980, 640),
                              confirm_close=True, text_select=True)
        _mac_branding()
        webview.start(localization={
            "global.quitConfirmation": "Uygulama kapatılsın mı? Çalışan bot durdurulur; açık pozisyonların "
                                       "stop emirleri Binance'te kalır.",
            "global.ok": "Kapat", "global.cancel": "Vazgeç", "global.quit": "Çık",
            "global.saveFile": "Dosyayı kaydet", "cocoa.menu.about": "Hakkında", "cocoa.menu.hide": "Gizle",
            "cocoa.menu.hideOthers": "Diğerlerini gizle", "cocoa.menu.showAll": "Tümünü göster",
            "cocoa.menu.quit": "TradingBot'tan çık", "cocoa.menu.edit": "Düzen", "cocoa.menu.view": "Görünüm",
            "cocoa.menu.fullscreen": "Tam ekran", "cocoa.menu.cut": "Kes", "cocoa.menu.copy": "Kopyala",
            "cocoa.menu.paste": "Yapıştır", "cocoa.menu.selectAll": "Tümünü seç"})
    log.info("Uygulama kapanıyor: motorlar durduruluyor (açık pozisyonların stopları borsada kalır)")
    app.engines.stop_all()
    httpd.shutdown()
    return 0
