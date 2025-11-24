#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import requests
import os
import csv
from datetime import datetime, timezone
from dateutil import tz
from statistics import mean, pstdev
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ================== SABİTLER ==================

MEXC_CONTRACT_BASE = "https://contract.mexc.com"

INTERVAL = "15m"          # 1m / 5m / 15m / 30m / 60m / 4h / 1d
LOOKBACK = 200            # Kaç mum geri bakılacak
REFRESH_SECONDS = 10      # Taramalar arası bekleme
PRICE_DECIMALS = 6        # Fiyat gösterim hassasiyeti

MIN_LONG_PCT = 1.5        # LONG için minimum potansiyel (%)
MIN_SHORT_PCT = 1.5       # SHORT için minimum potansiyel (%)

SL_ATR_MULT = 1.2         # Stop için ATR çarpanı
TP_ATR_MULT = 1.8         # TP için ATR çarpanı

RR_MIN = 1.2              # Minimum Risk/Ödül
TOP_N_VOLUME = 100        # En yüksek hacimli kaç kontrat taransın

LOG_FOLDER = "logs"
POSITIONS_FILE = "positions.csv"

last_alert_candle = {}
stats = {"long": 0, "short": 0, "cycles": 0}

# Bu döngüde açılan taze pozisyonlar – ilk turda TP/SL kontrol etmeyeceğiz
new_positions_this_cycle = set()

os.makedirs(LOG_FOLDER, exist_ok=True)


# ================== YARDIMCI / DOSYA / LOG ==================

def human_time_ms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz.tzlocal())
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def log_event(text: str):
    today = datetime.now().strftime("%Y-%m-%d")
    logfile = os.path.join(LOG_FOLDER, f"{today}.txt")
    with open(logfile, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def ensure_positions_file():
    if not os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "symbol", "direction", "entry", "sl", "tp1", "tp2",
                "signal_time", "rr", "status",
                "tp1_hit_time", "tp2_hit_time", "sl_hit_time"
            ])


def load_positions():
    ensure_positions_file()
    rows = []
    with open(POSITIONS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def save_positions(rows):
    ensure_positions_file()
    fieldnames = [
        "id", "symbol", "direction", "entry", "sl", "tp1", "tp2",
        "signal_time", "rr", "status",
        "tp1_hit_time", "tp2_hit_time", "sl_hit_time"
    ]
    with open(POSITIONS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def add_position(symbol, direction, entry, sl, tp1, tp2, signal_time_str, rr):
    ensure_positions_file()
    pos_id = f"{symbol}_{direction}_{signal_time_str.replace(' ', '_')}"
    with open(POSITIONS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            pos_id,
            symbol,
            direction,
            f"{entry:.8f}",
            f"{sl:.8f}",
            f"{tp1:.8f}",
            f"{tp2:.8f}",
            signal_time_str,
            f"{rr:.2f}",
            "PENDING",
            "", "", ""   # tp1_hit_time, tp2_hit_time, sl_hit_time
        ])
    return pos_id


# ================== MEXC API ==================

def mexc_interval(interval_str: str) -> str:
    mapping = {
        "1m": "Min1",
        "5m": "Min5",
        "15m": "Min15",
        "30m": "Min30",
        "60m": "Min60",
        "1h": "Min60",
        "4h": "Hour4",
        "1d": "Day1",
        "1D": "Day1",
    }
    return mapping.get(interval_str, "Min15")


def get_all_usdt_contracts():
    """
    Tüm MEXC USDT-M futures kontratlarını çeker.
    """
    url = f"{MEXC_CONTRACT_BASE}/api/v1/contract/detail"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    j = r.json()

    data = j.get("data", [])
    if isinstance(data, dict):
        data = [data]

    symbols = []
    for c in data:
        if c.get("quoteCoin") != "USDT":
            continue
        if c.get("settleCoin") != "USDT":
            continue
        if c.get("state") not in (0, None):  # 0 = normal
            continue
        sym = c.get("symbol")
        if not sym:
            continue
        symbols.append(sym)

    symbols = sorted(set(symbols))
    return symbols


def get_top_volume_contracts(allowed_symbols, top_n=TOP_N_VOLUME):
    """
    Tüm kontratların 24s hacimlerini çekip, ilk N tanesini döndürür.
    """
    url = f"{MEXC_CONTRACT_BASE}/api/v1/contract/ticker"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    j = r.json()

    data = j.get("data", [])
    if isinstance(data, dict):
        data = [data]

    vols = []
    for item in data:
        sym = item.get("symbol")
        if sym not in allowed_symbols:
            continue

        vol_raw = item.get("volume24") or item.get("amount24") or item.get("turnover24h") or 0
        try:
            vol = float(vol_raw)
        except (TypeError, ValueError):
            vol = 0.0

        vols.append((sym, vol))

    vols.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [sym for sym, vol in vols[:top_n]]
    return top_symbols


def get_klines(symbol):
    """
    MEXC Futures Kline datası.
    """
    url = f"{MEXC_CONTRACT_BASE}/api/v1/contract/kline/{symbol}"
    params = {"interval": mexc_interval(INTERVAL)}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()

    if not j.get("success", False):
        raise RuntimeError(f"Kline error for {symbol}: {j}")

    data = j.get("data", {})
    times = data.get("time", [])
    closes = data.get("close", [])
    highs = data.get("high", [])
    lows = data.get("low", [])

    n = min(LOOKBACK, len(closes))

    highs_f = [float(x) for x in highs][-n:]
    lows_f = [float(x) for x in lows][-n:]
    closes_f = [float(x) for x in closes][-n:]
    times_ms = [int(t) * 1000 for t in times][-n:]

    return highs_f, lows_f, closes_f, times_ms


# ================== İNDİKATÖRLER ==================

def sma(values, period):
    if len(values) < period:
        return None
    return float(mean(values[-period:]))


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[-(period + 1) + i] - closes[-(period + 2) + i]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None, None, None
    mid = sma(closes, period)
    window = closes[-period:]
    std = pstdev(window) if len(window) >= 2 else 0.0
    upper = mid + mult * std
    lower = mid - mult * std
    return lower, mid, upper


def atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        trs.append(tr)
    return sum(trs) / len(trs)


# ================== GÖRÜNÜM ==================

def stats_panel():
    table = Table(title="📊 İstatistik Paneli")
    table.add_column("Veri")
    table.add_column("Değer")
    table.add_row("Toplam Tarama", str(stats["cycles"]))
    table.add_row("LONG Sinyaller", str(stats["long"]))
    table.add_row("SHORT Sinyaller", str(stats["short"]))
    console.print(table)


def long_signal(symbol, price, sl, tp1, tp2, rr, close_time_ms):
    txt = [
        f"{symbol} LONG (MEXC Futures)",
        f"Giriş Fiyatı: {price:.{PRICE_DECIMALS}f}",
        f"Stop Loss:   {sl:.{PRICE_DECIMALS}f}",
        f"TP1:         {tp1:.{PRICE_DECIMALS}f}",
        f"TP2:         {tp2:.{PRICE_DECIMALS}f}",
        f"Risk/Ödül (RR): {rr:.2f}",
        f"Yakalanma Saati: {human_time_ms(close_time_ms)}"
    ]
    console.print(Panel("\n".join(txt), title="ZORA LONG", border_style="green"))

    signal_time_str = human_time_ms(close_time_ms)
    pos_id = add_position(symbol, "LONG", price, sl, tp1, tp2, signal_time_str, rr)
    new_positions_this_cycle.add(pos_id)

    log_event(
        f"[LONG] {symbol} | Entry:{price:.8f} | SL:{sl:.8f} | "
        f"TP1:{tp1:.8f} | TP2:{tp2:.8f} | RR:{rr:.2f} | Time:{signal_time_str}"
    )


def short_signal(symbol, price, sl, tp1, tp2, rr, close_time_ms):
    txt = [
        f"{symbol} SHORT (MEXC Futures)",
        f"Giriş Fiyatı: {price:.{PRICE_DECIMALS}f}",
        f"Stop Loss:   {sl:.{PRICE_DECIMALS}f}",
        f"TP1:         {tp1:.{PRICE_DECIMALS}f}",
        f"TP2:         {tp2:.{PRICE_DECIMALS}f}",
        f"Risk/Ödül (RR): {rr:.2f}",
        f"Yakalanma Saati: {human_time_ms(close_time_ms)}"
    ]
    console.print(Panel("\n".join(txt), title="ZORA SHORT", border_style="red"))

    signal_time_str = human_time_ms(close_time_ms)
    pos_id = add_position(symbol, "SHORT", price, sl, tp1, tp2, signal_time_str, rr)
    new_positions_this_cycle.add(pos_id)

    log_event(
        f"[SHORT] {symbol} | Entry:{price:.8f} | SL:{sl:.8f} | "
        f"TP1:{tp1:.8f} | TP2:{tp2:.8f} | RR:{rr:.2f} | Time:{signal_time_str}"
    )


# ================== POZİSYON TAKİBİ (TP1 / TP2 / SL) ==================

def update_positions(latest_ohlc):
    """
    latest_ohlc[symbol] = (close, high, low)
    """
    rows = load_positions()
    modified = False
    now_str = datetime.now(tz.tzlocal()).strftime("%Y-%m-%d %H:%M:%S")

    for row in rows:
        if "status" not in row:
            continue

        # Bu cycle'da yeni açılan pozisyonları ilk kontrolde atla
        if row.get("id") in new_positions_this_cycle:
            continue

        status = row["status"]
        if status not in ("PENDING", "TP1_HIT"):
            continue

        symbol = row["symbol"]
        direction = row["direction"]
        if symbol not in latest_ohlc:
            continue

        close_price, high_price, low_price = latest_ohlc[symbol]
        entry = float(row["entry"])
        sl = float(row["sl"])
        tp1 = float(row["tp1"])
        tp2 = float(row["tp2"])

        tp1_hit_time = row["tp1_hit_time"]
        tp2_hit_time = row["tp2_hit_time"]
        sl_hit_time = row["sl_hit_time"]

        # LONG: STOP low ile, TP high ile
        if direction == "LONG":
            # STOP
            if low_price <= sl and sl_hit_time == "":
                row["sl_hit_time"] = now_str
                row["status"] = "STOPPED"
                log_event(f"[RESULT] {symbol} LONG STOP | Entry:{entry} | SL:{sl} | Low:{low_price} | Time:{now_str}")
                modified = True
                continue

            # TP2
            if high_price >= tp2 and tp2_hit_time == "":
                if tp1_hit_time == "":
                    row["tp1_hit_time"] = now_str
                row["tp2_hit_time"] = now_str
                row["status"] = "CLOSED_TP2"
                log_event(f"[RESULT] {symbol} LONG TP2 | Entry:{entry} | TP2:{tp2} | High:{high_price} | Time:{now_str}")
                modified = True
                continue

            # TP1
            if high_price >= tp1 and tp1_hit_time == "":
                row["tp1_hit_time"] = now_str
                row["status"] = "TP1_HIT"
                log_event(f"[RESULT] {symbol} LONG TP1 | Entry:{entry} | TP1:{tp1} | High:{high_price} | Time:{now_str}")
                modified = True
                continue

        # SHORT: STOP high ile, TP low ile
        elif direction == "SHORT":
            # STOP
            if high_price >= sl and sl_hit_time == "":
                row["sl_hit_time"] = now_str
                row["status"] = "STOPPED"
                log_event(f"[RESULT] {symbol} SHORT STOP | Entry:{entry} | SL:{sl} | High:{high_price} | Time:{now_str}")
                modified = True
                continue

            # TP2
            if low_price <= tp2 and tp2_hit_time == "":
                if tp1_hit_time == "":
                    row["tp1_hit_time"] = now_str
                row["tp2_hit_time"] = now_str
                row["status"] = "CLOSED_TP2"
                log_event(f"[RESULT] {symbol} SHORT TP2 | Entry:{entry} | TP2:{tp2} | Low:{low_price} | Time:{now_str}")
                modified = True
                continue

            # TP1
            if low_price <= tp1 and tp1_hit_time == "":
                row["tp1_hit_time"] = now_str
                row["status"] = "TP1_HIT"
                log_event(f"[RESULT] {symbol} SHORT TP1 | Entry:{entry} | TP1:{tp1} | Low:{low_price} | Time:{now_str}")
                modified = True
                continue

    if modified:
        save_positions(rows)

    # Bu turda açılan pozisyonlar bir sonraki cycle’da kontrol edilecek
    new_positions_this_cycle.clear()


# ================== ANA DÖNGÜ ==================

def main():
    console.print("[bold green]SinyalBeyBot (MEXC Futures + Volume + Positions) başlatıldı[/bold green]")

    try:
        all_symbols = get_all_usdt_contracts()
    except Exception as e:
        console.print(f"[red]Kontrat listesi alınamadı:[/red] {e}")
        return

    console.print(f"[cyan]{len(all_symbols)} USDT-M kontrat bulundu.[/cyan]")

    try:
        top_symbols = get_top_volume_contracts(all_symbols, TOP_N_VOLUME)
    except Exception as e:
        console.print(f"[red]Hacim verisi alınamadı, tüm kontratlar taranacak:[/red] {e}")
        top_symbols = all_symbols

    symbols = top_symbols if top_symbols else all_symbols
    console.print(f"[cyan]Hacme göre ilk {len(symbols)} kontrat taranacak.[/cyan]")

    ensure_positions_file()

    while True:
        stats["cycles"] += 1
        latest_ohlc = {}

        open_positions = load_positions()  # açık pozisyon kontrolü için

        for symbol in symbols:
            try:
                highs, lows, closes, times = get_klines(symbol)
                if not closes:
                    continue

                price = closes[-1]
                high_last = highs[-1]
                low_last = lows[-1]
                close_time = times[-1]

                latest_ohlc[symbol] = (price, high_last, low_last)

                lower, mid, upper = bollinger(closes)
                r = rsi(closes)
                a = atr(highs, lows, closes)

                if any(v is None for v in (lower, upper, r, a)):
                    continue

                # Bu sembolde açık LONG / SHORT var mı?
                has_open_long = any(
                    (row["symbol"] == symbol and
                     row["direction"] == "LONG" and
                     row["status"] in ("PENDING", "TP1_HIT"))
                    for row in open_positions
                )
                has_open_short = any(
                    (row["symbol"] == symbol and
                     row["direction"] == "SHORT" and
                     row["status"] in ("PENDING", "TP1_HIT"))
                    for row in open_positions
                )

                # ===== LONG KOŞULU =====
                long_cond = (price < lower and r < 30)
                if long_cond and not has_open_long:
                    last = last_alert_candle.get(symbol + "_LONG")
                    if last != close_time:
                        sl = price - (SL_ATR_MULT * a)
                        tp2 = price + (TP_ATR_MULT * a)
                        tp1 = price + (tp2 - price) * 0.5

                        pct = (tp2 - price) / price * 100
                        if pct >= MIN_LONG_PCT:
                            risk = price - sl
                            reward = tp2 - price
                            rr = reward / risk if risk > 0 else 0.0

                            if rr >= RR_MIN:
                                long_signal(symbol, price, sl, tp1, tp2, rr, close_time)
                                stats["long"] += 1
                                last_alert_candle[symbol + "_LONG"] = close_time

                # ===== SHORT KOŞULU =====
                short_cond = (price > upper and r > 70)
                if short_cond and not has_open_short:
                    last = last_alert_candle.get(symbol + "_SHORT")
                    if last != close_time:
                        sl = price + (SL_ATR_MULT * a)
                        tp2 = price - (TP_ATR_MULT * a)
                        tp1 = price - (price - tp2) * 0.5

                        pct = (price - tp2) / price * 100
                        if pct >= MIN_SHORT_PCT:
                            risk = sl - price
                            reward = price - tp2
                            rr = reward / risk if risk > 0 else 0.0

                            if rr >= RR_MIN:
                                short_signal(symbol, price, sl, tp1, tp2, rr, close_time)
                                stats["short"] += 1
                                last_alert_candle[symbol + "_SHORT"] = close_time

            except Exception:
                continue

        # Açık pozisyonları güncelle (TP1 / TP2 / STOP)
        update_positions(latest_ohlc)

        stats_panel()
        console.print(f"Yeni tarama için {REFRESH_SECONDS} sn bekleniyor...\n")
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()