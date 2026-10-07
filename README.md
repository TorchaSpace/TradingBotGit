# TradingBot

Binance **spot** ve **USDⓈ-M futures** için otomatik trading botu. Backtest, risk yönetimi ve
paper / demo / live olmak üzere üç çalışma modu var.

> ⚠️ **Önemli:** Hiçbir bot kazancı garanti etmez. Bu stratejiler geçmiş veride test edildi ama
> geçmiş performans geleceği göstermez. Kripto, özellikle kaldıraçlı futures, yatırdığın paranın
> tamamını kaybettirebilir. Önce `paper`, sonra `demo`, en son ve küçük miktarla `live`.
> Bu proje yatırım tavsiyesi değildir.

## 🖥️ Masaüstü uygulaması (terminal gerekmez)
Finder'da proje klasöründeki **`TradingBot.app`**'e çift tıkla.
- **İlk açılış:** Gerekli her şeyi kendisi kurar (2-5 dk, bir bildirim çıkar), sonra pencere açılır.
  Mac'te Python yoksa macOS'un "Command Line Tools" kurulum penceresi çıkar, "Yükle" de ve uygulamayı
  tekrar aç. "Geliştiricisi doğrulanamadı" uyarısı çıkarsa: sağ tık → **Aç**.
- **Kurulum sihirbazı:** Hesap modu (Paper / Binance Demo / Gerçek), API anahtarları (bağlantı testi ve
  "para çekme izni açık mı" kontrolüyle), piyasa, risk profili, coinler, Telegram.
- **Panel:** Başlat / Durdur, özsermaye eğrisi, açık pozisyonlar, son işlemler, acil durdurma durumu.
- **Çıktı ve veriler:** Botun yaptığı her şey canlı akar. **Tüm verileri indir** tek zip oluşturur
  (`exports/`): işlem günlüğü, özsermaye, oturumlar, ayarlar, loglar, rapor. API anahtarları ve
  Telegram token'ı **otomatik silinir**. Geliştirme için Claude'a bu zip'i göndermen yeterli.
- **Ayarlar** ve **Hesaplar:** Tüm ayarlar açıklamalarıyla. Demo ↔ gerçek geçişi; gerçeğe geçmek için
  **GERÇEK PARA** yazman, her başlatmada da ayrıca onay vermen gerekir.
- **Piyasa taraması:** Botun şu an hangi coinde ne yapacağını gösterir, işlem açmaz.
- **Doğrulama:** Tek tıkla profesyonel testler (aşağıda): sonuçlar şans eseri mi, 12 ay için makul
  aralık ne, hangi risk seviyesinde ne kadar düşüş beklenir, bot gerçekte beklendiği gibi mi çalışıyor.
  Panel'de "Strateji sağlığı" satırı botun sonuçlarını bu beklentiyle sürekli karşılaştırır.

Uygulama açıkken bot çalışır. Pencere kapanınca durur, açık pozisyonların stop emirleri Binance'te
kalır. Uygulamayı Dock'a sürükleyebilir ya da `bash scripts/make_app.sh` ile Uygulamalar'a
ekleyebilirsin. Linux/Windows'ta: `python -m tradingbot app`.

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

## Varsayılan kurulum
`ema_trend` stratejisi + **BTC rejim filtresi**, **8 coin** (BTC, ETH, SOL, BNB, XRP, ADA, LINK, DOGE)
tek hesapta, 4 saatlik mum, **geniş stop (5×ATR)**, trailing kapalı. Risk seviyesi `PROFILE` ile:

| Profil | Risk/işlem | Yıllık getiri | En kötü düşüş | Sharpe | 2022 (ayı yılı) |
|---|---|---|---|---|---|
| `conservative` | %0.25 | %14.6 | -%7.7 | 1.58 | -%0.7 |
| `balanced` (varsayılan) | %0.5 | %28.7 | -%13.7 | 1.60 | -%1.5 |
| `aggressive` | %0.75 (4×ATR) | %53.1 | -%24.0 | 1.65 | -%3.3 |

*Spot, 2021-01 → 2026-10, ücret + kayma dahil. Futures `balanced`: yıllık %27.3, düşüş -%16.1,
her yıl artıda.*

**Gerçekçi beklenti:** Ayarlar 2021-2024'e bakılarak seçildi. Hiç bakılmadan saklanan
**2025-01 → 2026-10** döneminde `balanced` spot yıllık **%9.5**, düşüş **-%13.7** (futures %8.9,
-%12.3). Ayrıca bu 8 coin "bugün hâlâ büyük olanlar". 24 coinle (sonradan sönenler dahil) sonuçlar
daha zayıf. Yani tablodaki rakamlar iyimser, alt sınır değil.

**BTC rejim filtresi:** Bot yeni long'u sadece BTC 200 EMA'nın üstündeyken, futures'ta short'u sadece
altındayken açar. Altcoinler BTC düşerken yükselmeye çalışınca genelde başarısız oluyor. Hem eğitim hem
saklı dönemde düşüşü azaltıp getiriyi artıran tek ek buydu. Kapatmak için `.env`: `BTC_FILTER=false`.

**Eski ayarlarla kıyas** (%1 risk, 2×ATR trailing stop): aynı 8 coinde yıllık %1.2, düşüş **-%47**.

## İkinci motor: funding carry (isteğe bağlı)
Aynı coinde **spot long + futures short**. Fiyat hareketi birbirini götürür. Gelir, boğa
piyasasında futures short'lara ödenen **funding**'den gelir. Trend botuyla günlük getiri
korelasyonu **0.09**, yani farklı zamanlarda para kazanıyor.

| Dönem (23 coin) | Yıllık | En kötü düşüş |
|---|---|---|
| 2021-2024 | ~%10 | -%0.5 |
| 2025-2026 (saklı) | ~%0.4 | -%0.3 |

Yıllara göre: 2021 +%31.7, 2022 +%0.4, 2023 +%4.3, 2024 +%7.4, 2025 +%0.7. Funding sadece güçlü
boğa dönemlerinde yüksek. Diğer zamanlarda motor USDT'de bekler.

Trend %70 + carry %30 birleşik (2021-26): yıllık %22.2, düşüş **-%9.5** (sadece trend: %28.7 / -%13.3).
Getiri biraz azalıyor, düşüş belirgin azalıyor.

Riskler: short taraf fiyat sert yükselirse likidasyona gidebilir (motor short teminatının
%50'si erirse çifti kapatır), spot ve futures cüzdanının **ikisinde de** USDT gerekir.
```bash
python -m tradingbot carry-backtest           # geçmiş test
# .env: CARRY_CAPITAL=300  (carry'ye ayrılan USDT)
python -m tradingbot carry-run                # MODE=paper/demo/live ile aynı mantık
python -m tradingbot carry-flatten            # tüm carry çiftlerini kapat
```

## Ücretler
`BNB_FEE_DISCOUNT=true` (Binance'te "BNB ile öde" açık olmalı) ve `MAKER_FIRST=true` (girişte önce
limit emir, dolmazsa piyasa) ile yıllık getiri spot'ta %28.7 → ~%30, futures'ta %27.3 → ~%28.8.
Küçük ama bedava. Trend işlemleri uzun tutulduğu için ücret etkisi sınırlı.

## Bildirimler ve 7/24 çalışma
- **Telegram:** `.env`'ye `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` yaz, `python -m tradingbot notify-test`.
  Pozisyon açılış/kapanış, stop, kill switch, hata ve günlük özet mesajı gelir.
- **Mac'te arka planda + çökerse yeniden başlat:** `deploy/com.tradingbot.plist` (içinde kurulum
  komutları var). Mac uyursa bot durur.
- **VPS / Docker (önerilen, 7/24):** `docker compose up -d --build`. Carry için
  `docker compose --profile carry up -d`. Linux'ta Docker'sız: `deploy/tradingbot.service`.

## Gerçek parada güvenlik önlemleri
- **Her pozisyonun borsada stop emri var.** Bot kapansa bile stop borsada durur.
- **Yedek stop:** Fiyat stop'u geçtiği halde pozisyon hâlâ açıksa (spot stop-limit boşlukta dolmadı,
  borsa stop emrini reddetti vb.) bot pozisyonu kendisi piyasadan kapatır ve Telegram'a yazar.
  Reddedilen stop emirleri her döngüde tekrar denenir.
- **Bot sadece kendi açtığı pozisyonları yönetir.** Cüzdandaki kendi coinlerin, elle açtığın işlemler
  veya carry hedge'i sayılmaz, satılmaz, stop konmaz. Spot özsermaye hesabına da girmez.
- **Önce kayıt, sonra koruma:** Emir dolar dolmaz pozisyon kaydedilir. Stop koyarken hata olsa bile
  pozisyon "sahipsiz" kalmaz.
- **Bir coinde hata olursa diğerleri etkilenmez,** o coinin mumu bir sonraki döngüde tekrar işlenir.
- **Carry'de çıplak pozisyon kalmaz:** Önce short açılır. Spot alımı başarısız olursa short hemen
  geri kapatılır. Kapatırken de önce short kapanır.
- **Her işlem kaydedilir:** `state/journal_*.csv` (açılış, kapanış, sebep, tahmini PnL) ve saatlik
  özsermaye `state/equity_*.csv`.

⚠️ Carry motorunu, futures modunda çalışan trend botundan **ayrı bir Binance alt hesabında** çalıştır.
Futures pozisyonları coin başına nettir, aynı hesapta birbirini götürürler.

## Rapor paneli
```bash
python -m tradingbot report --open      # ya da: bash scripts/start.sh report
```
`reports/report.html` oluşturur: özsermaye eğrisi, getiri, en kötü düşüş, isabet, açık pozisyonlar
(stop'ları borsada mı), son 50 işlem ve backtest'in bu dönem için beklediği değerler. İnternetsiz
açılır, açık/koyu temaya uyar.

## Doğrulama: bu backtest'e ne kadar güvenebilirim?
```bash
python -m tradingbot validate --market spot --open     # ya da uygulamada: Doğrulama → Testleri çalıştır
```
`reports/validation_<piyasa>.html` oluşturur (30-90 sn). Fonların bir stratejiye para koymadan önce
yaptığı testler, mevcut ayarlarınla:

| Test | Ne sorar | Sonuç (spot, balanced) |
|---|---|---|
| Saklı dönem (2025-26) | Ayar seçiminde kullanılmamış veride çalışıyor mu? | Yıllık %9.4, düşüş -%13.7, Sharpe 0.74 |
| Deflated Sharpe Ratio (Bailey & López de Prado 2014) | ~200 varyant denendi; en iyisi şans eseri mi? | %44 (en sert, N=200) / %88 (gerçekçi, etkin N≈20) |
| PBO / CSCV (Bailey ve ark. 2017) | Komşu ayarlar arasından geçmişte en iyiyi seçmek işe yarıyor mu? | %60 → yaramıyor; bu yüzden sabit ayar |
| Parametre haritası | 16 EMA × stop kombinasyonu | Hepsi kârlı, Sharpe 1.43-1.66; saklı dönemde hepsi artıda |
| Maliyet stresi | Ücret ×2, kayma ×3 | Yıllık %24.3, Sharpe 1.39 |
| Monte Carlo (durağan blok bootstrap, 5.000 yıl) | Önümüzdeki 12 ay? | Kötümser: ortanca +%7.7, %5 ihtimalle -%12.7 veya kötü, **zararla kapatma olasılığı %31** |
| Risk seviyeleri | Risk artınca ne olur? | %0.5 → %20+ düşüş olasılığı %2; %1 → %41; %2 → %78 |
| Gerçek takip | Botun paper/demo/live sonuçları Monte Carlo aralığında mı? | Uygulamada canlı izlenir |

Futures'ta tablo daha zayıf: Deflated Sharpe %22 / %70, kötümser 12 ay zarar olasılığı %30.
Özet: avantaj büyük olasılıkla gerçek ama küçük; garanti yok, kötü bir yıl her zaman mümkün.
Not: EMA 50/150 saklı dönemde biraz daha iyi görünüyor (Sharpe 0.81 vs 0.74), ama bunu saklı döneme
bakarak seçmek tam da PBO'nun uyardığı hata olur; ayar değiştirilmedi.

## Neler denendi (`research/`)
Her fikir 2021-2024'te ve hiç kullanılmamış 2025-2026'da ayrı ayrı ölçüldü. Sadece ikisinde de işe
yarayanlar bota girdi.

| Deneme | Sonuç |
|---|---|
| Stop 2→5×ATR, trailing kapalı | ✅ En büyük etki (düşüş -%47 → -%15) |
| 8 coine yayma | ✅ Sadece BTC+ETH: Sharpe 1.12 → 8 coin: 1.50 |
| BTC rejim filtresi | ✅ Saklı dönemde spot Sharpe 0.65 → 0.74, futures 0.38 → 0.60 |
| 24 coin | ❌ Daha kötü (8 coinlik liste geriye dönük şanslı) |
| Düşüşte riski yarıya indirme | ❌ Saklı dönemde kötü |
| Breakeven stop, 4/6×ATR stop | ➖ Tutarlı değil (bir piyasada iyi, diğerinde kötü) |
| Spot + futures birlikte | ➖ %83 korelasyon, fayda yok |
| Funding carry (ayrı motor) | ✅ Korelasyon 0.09, birleşik düşüş -%13 → -%9.5. Ama 2025-26'da gelir neredeyse sıfır |
| Coinler arası momentum | ❌ 2021-24 yıllık %100+, ama düşüş -%55/-%70 ve 2025-26 sonucu çok kararsız |
| BNB indirimi + maker emir | ✅ Küçük ama garanti (+%1-1.5/yıl) |
| Parametre / strateji topluluğu (ensemble) | ➖ Saklı dönemde tek ayardan iyi değil (Sharpe 0.70-0.75 vs 0.74), mevcut ayar zaten sağlam |
| ADX / günlük trend filtresi | ➖ Fark yok |
| Kâr hedefi ile %70-90 isabet | ❌ İsabet %68-92'ye çıktı, getiri ~%0 veya eksi |
| Parametreleri 6 ayda bir yeniden optimize etme | ❌ Sabit ayardan kötü (Sharpe 1.15 vs 1.49) |
| ML: fiyat yönü tahmini | ❌ Doğruluk %50.9 = yazı-tura, tek başına -%42 |
| Daha hızlı işlem: 1 saat / 15 dk mumlar (`research/faster.py`) | ❌ Ayda 60-400 işlem, ama ücretler kârı siliyor: saklı dönemde spot 1h yıllık -%15, 15m -%77 (4h: +%9.5). Ücretsiz olsa kârlı olurdu |
| Zaman serisi momentumu, TSMOM (Moskowitz, Ooi, Pedersen 2012) | ❌ Saklı dönemde Sharpe 0.39-0.46 vs 0.74 |
| Volatiliteye göre risk (Moreira, Muir 2017) | ➖ Aynı ortalama riskle zamanlamasız sürümle kıyaslayınca: eğitimde kötü (1.89 vs 1.92), saklıda iyi (0.85 vs 0.75). Tutarlı değil |
| ML v2: hangi sinyalin kazanacağını tahmin (24 coin, ~10.000 işlem) | ➖ Gerçek bir sinyal var (AUC 0.60-0.66) ama portföyde tutarlı iyileşme yok. Futures'ta +%5 getiri, ama daha fazla düşüş. Spot'ta fayda yok. Etkin değil, veri biriktikçe `research/meta_labeling.py` ile tekrar dene |

## Risk kuralları (`.env` içinden ayarlanır, boş bırakılırsa profilden gelir)
| Ayar | balanced | Anlamı |
|---|---|---|
| `RISK_PER_TRADE` | 0.005 | Stop'a takılırsan özsermayenin en fazla %0.5'ini kaybedersin |
| `ATR_STOP_MULT` | 5.0 | Stop mesafesi = 5 × ATR |
| `TRAILING_STOP` | false | Stop'u fiyatla taşı (testlerde kapalı daha iyi) |
| `MAX_DAILY_LOSS` | 0.05 | Gün içinde %5 zarar → o gün yeni işlem açılmaz |
| `MAX_DRAWDOWN` | 0.25 | Zirveden %25 düşüş → bot pozisyonları kapatır ve **durur** |
| `MAX_OPEN_POSITIONS` | 6 | Aynı anda en fazla 6 pozisyon |
| `BTC_FILTER` | true | Long sadece BTC yükseliş trendindeyken, short sadece düşüşteyken |
| `MAX_LEVERAGE` | 3 | Futures toplam pozisyon üst sınırı (özsermayenin 3 katı). Spot'ta 1x |

Tüm pozisyonların toplam büyüklüğü özsermayeyi (futures'ta × kaldıraç) geçemez.
Stop'a takılan bir pozisyon, sinyal sıfırlanmadan aynı yönde tekrar açılmaz.
Spot'ta bot **sadece kendi aldığı coinleri** satar. Cüzdanındaki diğer coinlere dokunmaz.

## Stratejiler
- `ema_trend` (önerilen): EMA 20/50 trend takibi, EMA200 filtresiyle.
- `donchian_breakout`: 20 mumluk kanal kırılımına girer, 10 mumluk kanalda çıkar.
- `rsi_reversion`: Yükselen trendde RSI < 30 düşüşleri alır. Çok az işlem, düşük getiri.

## Kurulum (Mac / Linux), tek komut
```bash
cd ~/Desktop/TradingBot
bash scripts/setup.sh       # Python kontrolü, .venv, paketler, .env (paper), 55 test
bash scripts/start.sh       # botu başlatır (Mac'te uyku engellenir). Durdur: Ctrl+C veya `touch STOP`
```
Elle kurmak istersen: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
&& cp .env.example .env && pytest`.

Otomatik test (CI) için `deploy/github-actions-tests.yml` dosyasını `.github/workflows/tests.yml` konumuna taşıman yeterli; GitHub her push'ta testleri çalıştırır.

## Kullanım
```bash
# 1) Portföy backtest: canlı botla aynı kurallar, tek hesap (API anahtarı gerekmez)
python -m tradingbot portfolio --market spot --since 2021-01-01
python -m tradingbot portfolio --market spot --profile conservative
python -m tradingbot portfolio --market futures

#    Stratejileri coin coin karşılaştır
python -m tradingbot compare --market spot --since 2021-01-01

#    Araştırma scriptleri (ML için: pip install scikit-learn)
python research/holdout_tests.py spot futures
python research/xsec_momentum.py
python research/ensemble.py spot futures
python research/walk_forward.py spot
python research/ml_experiment.py spot
python research/meta_labeling.py
python research/literature.py spot futures      # TSMOM + volatiliteye göre risk

#    Profesyonel doğrulama raporu (Monte Carlo, Deflated Sharpe, PBO, risk seviyeleri)
python -m tradingbot validate --market spot --open

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
  live.py        paper / demo / live trend bot döngüsü
  carry.py       funding carry motoru (backtest + canlı)
  execution.py   emir gönderme (maker-önce limit, sonra piyasa)
  notify.py      Telegram bildirimleri
  journal.py     işlem günlüğü + özsermaye kaydı (state/*.csv)
  report.py      HTML rapor paneli (reports/report.html)
  validation.py  doğrulama: Monte Carlo, PSR / Deflated Sharpe, PBO, stres, risk seviyeleri, gerçek takip
  validation_report.py  reports/validation_<piyasa>.html
  app/           masaüstü uygulaması: yerel sunucu, arayüz (static/index.html), dışa aktarma
TradingBot.app/  Mac uygulama paketi (çift tıkla aç)
tests/           çevrimdışı testler (ağ / anahtar gerektirmez)
deploy/          Mac (launchd) ve Linux (systemd) servis dosyaları; Dockerfile + docker-compose.yml
scripts/         setup.sh (tek komut kurulum), start.sh (başlat / rapor)
research/        saklı-dönem testleri, walk-forward ve makine öğrenmesi deneyleri
```
`.env`, `state/`, `logs/`, `reports/`, `exports/`, `data/cache/` ve `backtests/results/` git'e gitmez.
