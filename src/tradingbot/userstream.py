"""Instant order updates over Binance's websocket (ccxt.pro, included in ccxt).

The bot normally checks the exchange every 30 seconds. With this stream running, a filled stop or
any other change to the account's orders wakes the bot immediately. It is an accelerator only:
if the stream cannot connect or keeps failing, it shuts itself down and the 30-second checks carry on.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

from .config import Settings

log = logging.getLogger(__name__)
FINAL = ("closed", "canceled", "cancelled", "expired", "rejected")
MAX_FAILURES = 5


def _default_factory(s: Settings):
    import ccxt.pro as pro
    cls = pro.binanceusdm if s.market == "futures" else pro.binance
    ex = cls({"apiKey": s.api_key, "secret": s.api_secret, "enableRateLimit": True,
              "options": {"adjustForTimeDifference": True}})
    if s.mode == "demo":
        ex.enable_demo_trading(True)
    return ex


class UserStream:
    def __init__(self, s: Settings, on_event: Callable[[dict], None], factory=None):
        self.s, self.on_event = s, on_event
        self.factory = factory or _default_factory
        self.connected = False
        self.events = 0
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> "UserStream":
        self.thread = threading.Thread(target=self._run, name="user-stream", daemon=True)
        self.thread.start()
        return self

    def stop(self, wait: float = 5.0) -> None:
        self._stop.set()
        if self._loop is not None and self._task is not None:
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:  # loop already closed
                pass
        if self.thread is not None:
            self.thread.join(wait)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self._main())
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("user stream crashed (normal 30 s checks continue)")
        finally:
            self.connected = False
            self._loop.close()

    async def _main(self) -> None:
        try:
            ex = self.factory(self.s)
        except Exception as e:
            log.warning("Anlık emir bildirimi başlatılamadı (%s); normal kontrol devam ediyor.", e)
            return
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    orders = await ex.watch_orders()
                    if not self.connected:
                        log.info("Anlık emir bildirimi bağlı (WebSocket)")
                    self.connected, failures = True, 0
                    for o in orders or []:
                        if str(o.get("status")) in FINAL:
                            self.events += 1
                            self.on_event(o)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failures += 1
                    self.connected = False
                    if failures >= MAX_FAILURES:
                        log.warning("Anlık emir bildirimi %d kez koptu, kapatıldı; bot 30 sn'lik kontrolle devam "
                                    "ediyor. Son hata: %s", failures, e)
                        return
                    log.info("Anlık emir bildirimi koptu (%s), %d sn sonra tekrar", e, 2 ** failures)
                    await asyncio.sleep(2 ** failures)
        finally:
            try:
                await ex.close()
            except Exception:
                pass
