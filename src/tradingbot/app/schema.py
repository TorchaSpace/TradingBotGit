"""Settings shown in the desktop app: label, help text, type and grouping for every .env key."""
from __future__ import annotations

from ..config import CARRY_DEFAULT_BASES, DEFAULT_SYMBOLS, PROFILES

ALL_COINS = list(CARRY_DEFAULT_BASES)
SECRET_KEYS = {"BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_API_SECRET",
               "BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET", "TELEGRAM_BOT_TOKEN"}

# type: choice | number | percent (stored as fraction, shown as %) | bool | coins | text | secret
FIELDS = [
    # ---- Hesap
    dict(key="MODE", group="account", label="Çalışma modu", type="choice", default="paper",
         choices=[["paper", "Paper (sahte para, anahtar gerekmez)"],
                  ["demo", "Binance Demo (sahte para, gerçek borsa)"],
                  ["live", "GERÇEK HESAP (gerçek para)"]],
         help="Önce paper, sonra demo. Gerçek hesaba geçiş ayrıca onay ister."),
    dict(key="BINANCE_DEMO_API_KEY", group="account", label="Demo API Key", type="secret",
         help="demo.binance.com → API Management'ta oluşturulur."),
    dict(key="BINANCE_DEMO_API_SECRET", group="account", label="Demo Secret Key", type="secret", help=""),
    dict(key="BINANCE_LIVE_API_KEY", group="account", label="Gerçek hesap API Key", type="secret",
         help="Binance → API Management. Withdrawals KAPALI, IP kısıtlaması AÇIK olsun."),
    dict(key="BINANCE_LIVE_API_SECRET", group="account", label="Gerçek hesap Secret Key", type="secret", help=""),
    dict(key="PAPER_START_BALANCE", group="account", label="Paper başlangıç bakiyesi (USDT)", type="number",
         default="1000", min=50, max=10_000_000, step=50, help="Sadece paper modunda kullanılır."),

    # ---- Strateji
    dict(key="MARKET", group="strategy", label="Piyasa", type="choice", default="spot",
         choices=[["spot", "Spot (sadece alım, kaldıraçsız)"],
                  ["futures", "Futures (long + short, kaldıraç sınırlı)"]],
         help="Spot daha basit ve likidasyon riski yok."),
    dict(key="STRATEGY", group="strategy", label="Strateji", type="choice", default="ema_trend",
         choices=[["ema_trend", "EMA trend (önerilen)"], ["donchian_breakout", "Donchian kırılım"],
                  ["rsi_reversion", "RSI dönüş (az işlem)"]], help=""),
    dict(key="LEARNER_MODE", group="strategy", label="Öğrenen model", type="choice", default="shadow",
         choices=[["shadow", "Gölge: sadece puanlar, işlemlere karışmaz (önerilen)"],
                  ["filter", "Filtre: onaylıysa en zayıf sinyalleri atlar"], ["off", "Kapalı"]],
         help="Model her hafta tüm geçmiş + yeni verilerle yeniden eğitilir. Filtre, ancak geçmiş testlerde "
              "işe yaradığı kanıtlanırsa (onaylı) devreye girer; onaylı değilse bot normal çalışır."),
    dict(key="PAIR_FILTER", group="strategy", label="Coin güvenlik filtresi", type="bool", default="true",
         help="Binance'in işleme kapattığı ya da listeden çıkaracağını duyurduğu, 24 saatlik hacmi düşük ya da 90 günden "
              "yeni coinlerde yeni işlem açılmaz. Listeden çıkacak bir coindeki pozisyon 7 gün kala kapatılır."),
    dict(key="MIN_QUOTE_VOLUME", group="strategy", label="En düşük 24 saatlik hacim (USDT)", type="number",
         default="5000000", min=0, step=1000000, advanced=True, help="Bu hacmin altındaki coinlerde yeni işlem açılmaz."),
    dict(key="SYMBOLS", group="strategy", label="İşlem yapılacak coinler", type="coins",
         default=",".join(DEFAULT_SYMBOLS), choices=ALL_COINS,
         help="Testlerde en iyi sonuç 8 büyük coinle alındı. Daha fazla coin = daha fazla çeşitlilik ama "
              "daha zayıf coinler."),
    dict(key="BTC_FILTER", group="strategy", label="BTC trend filtresi", type="bool", default="true",
         help="Yeni long sadece BTC yükseliş trendindeyken açılır. Testlerde kaybı azalttı."),
    dict(key="TIMEFRAME", group="strategy", label="Mum aralığı", type="choice", default="4h",
         choices=[["4h", "4 saat (test edilen)"], ["1h", "1 saat (test edilmedi)"],
                  ["1d", "1 gün (test edilmedi)"]], advanced=True, help=""),

    # ---- Risk
    dict(key="PROFILE", group="risk", label="Risk profili", type="choice", default="balanced",
         choices=[["conservative", "Temkinli: %0.25 risk/işlem"], ["balanced", "Dengeli: %0.5 risk/işlem"],
                  ["aggressive", "Agresif: %0.75 risk/işlem"]],
         help="Aşağıdaki ayarlar boş bırakılırsa profilin değerleri kullanılır."),
    dict(key="RISK_PER_TRADE", group="risk", label="İşlem başına risk", type="percent", min=0.05, max=5,
         step=0.05, placeholder_from="risk_per_trade",
         help="Stop'a takılırsa kaybedilecek özsermaye yüzdesi."),
    dict(key="ATR_STOP_MULT", group="risk", label="Stop mesafesi (× ATR)", type="number", min=1, max=10,
         step=0.5, placeholder_from="atr_stop_mult", help="Testlerde 4-5 en iyisiydi. Dar stop = gereksiz stoplar."),
    dict(key="MAX_OPEN_POSITIONS", group="risk", label="En fazla açık pozisyon", type="number", min=1, max=20,
         step=1, placeholder_from="max_open_positions", help=""),
    dict(key="MAX_DAILY_LOSS", group="risk", label="Günlük zarar limiti", type="percent", min=0.5, max=20,
         step=0.5, placeholder_from="max_daily_loss", help="Aşılınca o gün yeni işlem açılmaz."),
    dict(key="MAX_DRAWDOWN", group="risk", label="Acil durdurma (zirveden düşüş)", type="percent", min=2, max=50,
         step=1, placeholder_from="max_drawdown", help="Aşılınca bot pozisyonları kapatır ve durur."),
    dict(key="MAX_LEVERAGE", group="risk", label="Futures kaldıraç üst sınırı", type="number", min=1, max=10,
         step=1, default="3", help="Spot'ta her zaman 1×."),
    dict(key="TRAILING_STOP", group="risk", label="Takip eden stop", type="bool", default="false",
         advanced=True, help="Testlerde kapalı daha iyiydi."),

    # ---- Ücret / emir
    dict(key="BNB_FEE_DISCOUNT", group="fees", label="Ücretleri BNB ile öde (-%25)", type="bool",
         default="false", help="Binance'te de 'BNB ile öde' açık ve hesapta biraz BNB olmalı."),
    dict(key="MAKER_FIRST", group="fees", label="Önce limit emir dene", type="bool", default="false",
         help="Girişte önce maker (düşük ücret) emir; dolmazsa piyasa emri. Çıkışlar her zaman piyasa."),
    dict(key="MAX_SPREAD", group="fees", label="En fazla alış-satış farkı", type="percent", default="0.003",
         min=0.05, max=5, step=0.05, advanced=True,
         help="Girişten önce emir defterine bakılır; fark bundan büyükse işlem açılmaz, sonra tekrar denenir."),
    dict(key="MAX_IMPACT", group="fees", label="En fazla beklenen kayma", type="percent", default="0.005",
         min=0.1, max=5, step=0.05, advanced=True,
         help="Emir defteri sığsa (büyük emir fiyatı çok oynatacaksa) giriş ertelenir. Çıkışlar ve stop'lar hiç beklemez."),
    dict(key="PRICE_CHECK", group="fees", label="Fiyatı ikinci borsayla kontrol et (gerçek hesap)", type="bool",
         default="true", advanced=True,
         help="Gerçek hesapta girişten önce Binance fiyatı Coinbase/Kraken ile karşılaştırılır; %2'den fazla fark varsa işlem açılmaz."),
    dict(key="USER_STREAM", group="fees", label="Anlık emir bildirimi (WebSocket)", type="bool", default="true",
         advanced=True, help="Stop dolunca bot bir sonraki kontrolü beklemeden hemen tepki verir. Bağlantı koparsa normal kontrole döner."),
    dict(key="MAKER_WAIT_SECONDS", group="fees", label="Limit emir bekleme (sn)", type="number", default="45",
         min=5, max=300, step=5, advanced=True, help=""),
    dict(key="FEE_RATE", group="fees", label="Ücret oranı (boş = Binance standart)", type="percent",
         min=0, max=1, step=0.005, advanced=True, help="Spot %0.1, futures %0.05."),

    # ---- Bildirim
    dict(key="TELEGRAM_BOT_TOKEN", group="notify", label="Telegram bot token", type="secret",
         help="@BotFather → /newbot"),
    dict(key="TELEGRAM_CHAT_ID", group="notify", label="Telegram chat ID", type="text",
         help="Bota bir mesaj at, sonra 'Chat ID bul' butonuna bas."),

    # ---- Carry
    dict(key="CARRY_CAPITAL", group="carry", label="Carry sermayesi (USDT, 0 = kapalı)", type="number",
         default="0", min=0, max=10_000_000, step=50,
         help="Spot long + futures short. Spot ve futures cüzdanında USDT gerekir."),
    dict(key="CARRY_SLOTS", group="carry", label="Aynı anda en fazla coin", type="number", default="5",
         min=1, max=20, step=1, help=""),
    dict(key="CARRY_LEVERAGE", group="carry", label="Short tarafı kaldıracı", type="number", default="2",
         min=1, max=3, step=1, help="2 = sermayenin 2/3'ü spot, 1/3'ü teminat."),
    dict(key="CARRY_ENTER", group="carry", label="Giriş eşiği (8 saatlik funding)", type="percent",
         default="0.0001", min=0, max=0.2, step=0.001, advanced=True, help="%0.01 ≈ yıllık %11."),
]

GROUPS = [
    ("account", "Hesap"), ("strategy", "Strateji"), ("risk", "Risk"), ("fees", "Ücret ve emir"),
    ("notify", "Bildirimler"), ("carry", "Funding carry"),
]

PROFILE_INFO = {k: v for k, v in PROFILES.items()}
