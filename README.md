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
3. (İsteğe bağlı, varsayılan kapalı) stop'u fiyatla birlikte taşır.
4. Yeni sinyal varsa ve risk kuralları izin veriyorsa pozisyon açar ve **Binance tarafında**
   stop-loss emri koyar. Bot kapansa bile stop borsada durur.

## Varsayılan kurulum (optimizasyon sonrası)
`ema_trend` stratejisi, **8 coin** (BTC, ETH, SOL, BNB, XRP, ADA, LINK, DOGE) tek hesapta, 4 saatlik
mum, **geniş stop (5×ATR)**, trailing kapalı. Risk seviyesi `PROFILE` ile seçilir:

| Profil | Risk/işlem | Yıllık getiri | En kötü düşüş | Sharpe | 2022 (ayı yılı) |
|---|---|---|---|---|---|
| `conservative` | %0.25 | %13.5 | -%8.2 | 1.47 | -%2.1 |
| `balanced` (varsayılan) | %0.5 | %27.3 | -%15.5 | 1.50 | -%4.5 |
| `aggressive` | %0.75 (4×ATR) | %49.8 | -%26.5 | 1.54 | -%8.6 |

*Spot, 2021-01 → 2026-10, ücret + kayma dahil, `python -m tradingbot portfolio` ile.
Futures `balanced`: yıllık %21.5, düşüş -%15.4, 2022 dahil her yıl artıda.*
Son iki yıl (2025-2026) belirgin şekilde zayıf: yılda +%7 civarı.

**Eski ayarlarla kıyas** (%1 risk, 2×ATR trailing stop): aynı 8 coinde yıllık %1.2, düşüş **-%47**.
Asıl kazanç stop mesafesinden geldi. Dar stop, gürültüde sürekli tetiklenip trendin
büyük kısmını kaçırıyordu.

## Neler denendi, neler işe yaramadı (`research/`)
| Deneme | Sonuç |
|---|---|
| Stop mesafesi 2→4-6×ATR, trailing kapalı | ✅ Her 8 coinde iyileşme, en büyük etki |
| 8 coine yayma (tek hesap) | ✅ Sadece BTC+ETH: Sharpe 1.12, yıllık %6. 8 coin: Sharpe 1.50, yıllık %27 |
| ADX (trend gücü) filtresi | ➖ İşlem sayısı azaldı, getiri/Sharpe düştü |
| Günlük grafik trend filtresi | ➖ Belirgin fark yok |
| Her 6 ayda parametreleri yeniden optimize etme | ❌ Sabit ayardan **kötü**: spot Sharpe 1.15 vs 1.49, düşüş -%24 vs -%16. Geçmişe aşırı uyum |
| Makine öğrenmesi ile 2 günlük yön tahmini | ❌ Görülmemiş veride doğruluk %50.9 (yazı-tura %50). Tek başına ücretlerden sonra **-%42**. Filtre olarak isabeti artırdı ama toplam getiriyi artırmadı |

Sonuç: Fiyatı "bilen" bir model yok. Kaybı azaltan şeyler doğru stop mesafesi, çeşitlendirme ve
pozisyon büyüklüğü. İsabet oranı ~%20, ama kazanan işlem ortalama kaybedenin ~8 katı (profit factor 2.1).

## Risk kuralları (`.env` içinden ayarlanır, boş bırakılırsa profilden gelir)
| Ayar | balanced | Anlamı |
|---|---|---|
| `RISK_PER_TRADE` | 0.005 | Stop'a takılırsan özsermayenin en fazla %0.5'ini kaybedersin |
| `ATR_STOP_MULT` | 5.0 | Stop mesafesi = 5 × ATR |
| `TRAILING_STOP` | false | Stop'u fiyatla taşı (testlerde kapalı daha iyi) |
| `MAX_DAILY_LOSS` | 0.05 | Gün içinde %5 zarar → o gün yeni işlem açılmaz |
| `MAX_DRAWDOWN` | 0.25 | Zirveden %25 düşüş → bot pozisyonları kapatır ve **durur** |
| `MAX_OPEN_POSITIONS` | 6 | Aynı anda en fazla 6 pozisyon |
| `MAX_LEVERAGE` | 3 | Futures toplam pozisyon üst sınırı (özsermayenin 3 katı). Spot'ta 1x |

Tüm pozisyonların toplam büyüklüğü özsermayeyi (futures'ta × kaldıraç) geçemez.
Stop'a takılan bir pozisyon, sinyal sıfırlanmadan aynı yönde tekrar açılmaz.
Spot'ta bot **sadece kendi aldığı coinleri** satar. Cüzdanındaki diğer coinlere dokunmaz.

## Stratejiler
- `ema_trend` (önerilen): EMA 20/50 trend takibi, EMA200 filtresiyle.
- `donchian_breakout`: 20 mumluk kanal kırılımına girer, 10 mumluk kanalda çıkar.
- `rsi_reversion`: Yükselen trendde RSI < 30 düşüşleri alır. Çok az işlem, düşük getiri.

## Kurulum (Mac)
```bash
cd ~/Desktop/TradingBot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # sonra .env dosyasını düzenle
pytest                      # 34 test geçmeli
```

## Kullanım
```bash
# 1) Portföy backtest: canlı botla aynı kurallar, tek hesap (API anahtarı gerekmez)
python -m tradingbot portfolio --market spot --since 2021-01-01
python -m tradingbot portfolio --market spot --profile conservative
python -m tradingbot portfolio --market futures

#    Stratejileri coin coin karşılaştır
python -m tradingbot compare --market spot --since 2021-01-01

#    Araştırma scriptleri (ML için: pip install scikit-learn)
python research/walk_forward.py spot
python research/ml_experiment.py spot

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
4. Mac uyku moduna geçerse bot durur. Stop emirleri borsada kalır ama sinyalle çıkış
   çalışmaz. Çalışırken uykuyu engellemek için: `caffeinate -i python -m tradingbot run`

## Proje yapısı
```
src/tradingbot/
  config.py      ayarlar, canlı mod kilidi
  exchange.py    Binance bağlantısı (ccxt), demo modu, retry / rate limit
  data.py        geçmiş mum indirme + cache (data/cache/)
  indicators.py  EMA, RSI, ATR, Donchian, ADX, günlük trend
  strategies.py  stratejiler (saf fonksiyonlar)
  risk.py        pozisyon boyutu, stop, günlük zarar / kill switch
  backtest.py    tek coin + portföy (tek hesap) backtest motoru, metrikler
  live.py        paper / demo / live bot döngüsü
tests/           çevrimdışı testler (ağ / anahtar gerektirmez)
research/        walk-forward ve makine öğrenmesi deneyleri
```
`.env`, `state/`, `logs/`, `data/cache/` ve `backtests/results/` git'e gitmez.
