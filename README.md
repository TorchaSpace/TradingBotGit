# TradingBot

Binance **spot** ve **USDⓈ-M futures** için otomatik trading botu. Backtest, risk yönetimi ve
paper / demo / live olmak üzere üç çalışma modu var.

> ⚠️ **Önemli:** Hiçbir bot kazancı garanti etmez. Bu stratejiler geçmiş veride test edildi ama
> geçmiş performans geleceği göstermez. Kripto, özellikle kaldıraçlı futures, yatırdığın paranın
> tamamını kaybettirebilir. Önce `paper`, sonra `demo`, en son ve küçük miktarla `live`.
> Bu proje yatırım tavsiyesi değildir.

## Nasıl çalışır?

Bot Binance'e web sitesi üzerinden değil, resmi **API** ile bağlanır. Şifren bota hiç girilmez.
Sadece bir API anahtarı verirsin ve bu anahtarda **çekim (withdraw) yetkisi kapalı** olur.
Yani bot al-sat yapabilir ama hesabından para çekemez.

Her yeni kapanan mumda (varsayılan 4 saatlik) bot şunları yapar:
1. Strateji sinyalini sadece **kapanmış** mumlardan hesaplar (geleceğe bakma yok).
2. Sinyal pozisyonla uyuşmuyorsa pozisyonu kapatır.
3. Açık pozisyonun stop'unu ATR'ye göre yukarı (short'ta aşağı) taşır (trailing stop).
4. Yeni sinyal varsa ve risk kuralları izin veriyorsa pozisyon açar ve **Binance tarafında**
   stop-loss emri koyar. Bot kapansa bile stop borsada durur.

## Risk kuralları (`.env` içinden ayarlanır)
| Ayar | Varsayılan | Anlamı |
|---|---|---|
| `RISK_PER_TRADE` | 0.01 | Stop'a takılırsan özsermayenin en fazla %1'ini kaybedersin |
| `ATR_STOP_MULT` | 2.0 | Stop mesafesi = 2 × ATR |
| `MAX_DAILY_LOSS` | 0.03 | Gün içinde %3 zarar → o gün yeni işlem açılmaz |
| `MAX_DRAWDOWN` | 0.15 | Zirveden %15 düşüş → bot pozisyonları kapatır ve **durur** |
| `MAX_OPEN_POSITIONS` | 2 | Aynı anda en fazla 2 pozisyon |
| `MAX_LEVERAGE` | 3 | Futures kaldıraç üst sınırı (en fazla 10'a izin verilir). Spot'ta her zaman 1x |

Stop'a takılan bir pozisyon, sinyal sıfırlanmadan aynı yönde tekrar açılmaz.
Spot'ta bot **sadece kendi aldığı coinleri** satar. Cüzdanındaki diğer coinlere dokunmaz.

## Stratejiler
- `ema_trend`: EMA 20/50 trend takibi, EMA200 filtresiyle.
- `rsi_reversion`: Yükselen trendde RSI < 30 olan düşüşleri alır (futures'ta tersi short).
- `donchian_breakout`: 20 mumluk kanal kırılımına girer, 10 mumluk kanalda çıkar.

### Backtest özeti (4h, 2021-01 → 2026-10)
Hesaba katılanlar: %0.1 spot / %0.05 futures ücret, %0.05 kayma, futures için funding maliyeti,
%1 risk/işlem. "OUT 30%" son %30'luk dönem (yaklaşık 2025-01 → 2026-10).

| Piyasa | Sembol | Strateji | Toplam getiri | Son %30 | Max düşüş | İsabet |
|---|---|---|---|---|---|---|
| Spot | BTC | ema_trend | +53% | +12.5% | -10% | %31 |
| Spot | BTC | donchian_breakout | +61% | +4.4% | -15% | %34 |
| Spot | ETH | ema_trend | +24% | +6.7% | -11% | %30 |
| Spot | ETH | donchian_breakout | -22% | -13.7% | -23% | %24 |
| Futures | BTC | ema_trend | +71% | +19.6% | -11% | %29 |
| Futures | ETH | ema_trend | +32% | +0.7% | -17% | %28 |
| Futures | SOL | rsi_reversion | +7.6% | +4.2% | -3.5% | %50 |

Aynı dönemde BTC'yi alıp tutmak (+193%) çoğu stratejiden fazla kazandırdı, ama -%50'ye varan
düşüşlerle. Botun amacı düşüşü sınırlı tutmak. İsabet oranı ~%30 olsa bile kazançlı işlemler
kayıplıların ~3-4 katı büyük olduğu için toplamda kâr çıkıyor. Tam tablo için
`python -m tradingbot compare` çalıştır.

## Kurulum (Mac)
```bash
cd ~/Desktop/TradingBot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # sonra .env dosyasını düzenle
pytest                      # 27 test geçmeli
```

## Kullanım
```bash
# 1) Stratejileri geçmiş veride karşılaştır (API anahtarı gerekmez)
python -m tradingbot compare --market spot --symbols BTC/USDT,ETH/USDT --since 2021-01-01
python -m tradingbot compare --market futures --symbols BTC/USDT,ETH/USDT

# 2) Tek strateji backtest + işlem listesi (backtests/results/ içine kaydeder)
python -m tradingbot backtest --strategy ema_trend --symbols BTC/USDT

# 3) Paper mod: gerçek fiyatlar, sahte para (MODE=paper)
python -m tradingbot run

# 4) Binance Demo (sahte para, gerçek borsa altyapısı)
#    https://demo.binance.com/en/my/settings/api-management adresinden demo API anahtarı al,
#    .env içinde MODE=demo yap ve anahtarları yaz
python -m tradingbot check
python -m tradingbot run

# Botu durdurmak: Ctrl+C ya da proje klasöründe STOP adında bir dosya oluştur
touch STOP      # (tekrar başlatmadan önce: rm STOP)

# Botun açtığı tüm pozisyonları hemen kapat
python -m tradingbot flatten

# Kill switch tetiklendiyse, durumu inceledikten sonra
python -m tradingbot reset-halt
```

## Canlıya geçiş (gerçek para)
Bu adımı ancak demo'da en az birkaç hafta sorunsuz çalıştıktan sonra düşün.
1. Mümkünse Binance'te bot için ayrı bir **alt hesap (sub-account)** aç ve sadece riske
   edebileceğin bir miktar koy.
2. API anahtarı oluştururken: **Enable Withdrawals KAPALI**, **IP erişim kısıtlaması AÇIK**.
   Futures kullanacaksan "Enable Futures" açık olmalı.
3. `.env`: `MODE=live` ve `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK`. Bu satır yoksa bot
   canlıda çalışmayı reddeder.
4. Mac uyku moduna geçerse bot durur. Stop emirleri borsada kalır ama sinyalle çıkış ve trailing
   çalışmaz. Çalışırken uykuyu engellemek için: `caffeinate -i python -m tradingbot run`

## Proje yapısı
```
src/tradingbot/
  config.py      ayarlar, canlı mod kilidi
  exchange.py    Binance bağlantısı (ccxt), demo modu, retry / rate limit
  data.py        geçmiş mum indirme + cache (data/cache/)
  indicators.py  EMA, RSI, ATR, Donchian
  strategies.py  stratejiler (saf fonksiyonlar)
  risk.py        pozisyon boyutu, trailing stop, günlük zarar / kill switch
  backtest.py    backtest motoru + metrikler
  live.py        paper / demo / live bot döngüsü
tests/           çevrimdışı testler (ağ / anahtar gerektirmez)
```
`.env`, `state/`, `logs/`, `data/cache/` ve `backtests/results/` git'e gitmez.
