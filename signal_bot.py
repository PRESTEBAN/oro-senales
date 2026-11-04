#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oro SMC V8 Apex Lab -> avisos por Telegram en M15, H1 y H4.
Corre en GitHub Actions, disparado desde cron-job.org.

PORT 1:1 DEL INDICADOR PINE "Oro Sniper SMC - Alpha Green V8 Apex Lab":
  * Cero retraso: la señal se genera en la MISMA vela que produce el rechazo (vela 1).
    Entrada = cierre de la vela 1 (= apertura de la vela 2).
  * Cero repintado: solo se evalúan velas YA CERRADAS (barstate.isconfirmed).
  * SL protegido al extremo: ventas -> high + 0.40 x ATR ; compras -> low - 0.40 x ATR.
  * Alerta instantánea al corte.

Temporalidades soportadas:
  M15 -> perfil base (mecha 0.40, cooldown 10, espacio 18 velas / 1.1 ATR, ADX min 16, HTF 4H)
  H1  -> perfil H1   (mecha 0.40, cooldown 7,  espacio 12 velas / 0.85 ATR, ADX min 13, HTF 4H)
  H4  -> perfil H4+  (mecha 0.40, cooldown 10, espacio 18 velas / 1.1 ATR, ADX min 18, HTF DIARIO)
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================================
# Configuración general
# ============================================================================
NY = ZoneInfo("America/New_York")
EC = ZoneInfo("America/Guayaquil")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TF_INFO = {
    15: dict(name="M15", label="15m", res="MINUTE_15", td="15min", days=10, win=3),
    60: dict(name="H1", label="1h", res="HOUR", td="1h", days=35, win=2),
    240: dict(name="H4", label="4h", res="HOUR_4", td="4h", days=120, win=3),
}
BAR = timedelta(minutes=15)
TF = 15


def set_tf(minutes):
    global BAR, TF
    TF = minutes
    BAR = timedelta(minutes=minutes)


def tf_name():
    return TF_INFO[TF]["name"]


def parse_timeframes(text):
    out = []
    for x in text.replace(" ", "").split(","):
        if not x:
            continue
        if not x.isdigit() or int(x) not in TF_INFO:
            sys.exit("TIMEFRAMES solo admite 15, 60 y 240 (separados por coma). Recibí: %s" % text)
        if int(x) not in out:
            out.append(int(x))
    if not out:
        sys.exit("TIMEFRAMES está vacío")
    return out


def env_bool(name, default):
    return os.getenv(name, "1" if default else "0").strip() not in ("0", "false", "False", "")


TIMEFRAMES = parse_timeframes(os.getenv("TIMEFRAMES", "15,60,240"))
DATA_SOURCE = os.getenv("DATA_SOURCE", "capital").strip().lower()   # capital | twelvedata
SYMBOL = os.getenv("SYMBOL", "XAU/USD")
OUTPUTSIZE = int(os.getenv("OUTPUTSIZE", "5000"))
CAPITAL_EPIC = os.getenv("CAPITAL_EPIC", "GOLD")
CAPITAL_PRICE = os.getenv("CAPITAL_PRICE", "bid").strip().lower()      # bid | mid | ask
CAPITAL_HOSTS = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}
CAPITAL_ENV_USED = ""
_CAPITAL_SESSION = None
WINDOWS_EXTENDED = int(os.getenv("WINDOWS_EXTENDED", "6"))

if DATA_SOURCE not in ("capital", "twelvedata"):
    sys.exit("DATA_SOURCE debe ser capital o twelvedata")
if CAPITAL_PRICE not in ("bid", "mid", "ask"):
    sys.exit("CAPITAL_PRICE debe ser bid, mid o ask")

LOOKBACK_BARS = int(os.getenv("LOOKBACK_BARS", "1"))
MAX_AGE_MIN = float(os.getenv("MAX_AGE_MIN", "45"))
CLOSE_MARGIN_S = float(os.getenv("CLOSE_MARGIN_S", "3"))
MAX_WAIT_S = float(os.getenv("MAX_WAIT_S", "100"))
FRESH_RETRIES = int(os.getenv("FRESH_RETRIES", "3"))


def parse_session(text):
    try:
        a, b = text.replace(" ", "").split("-")
        h1, m1 = [int(x) for x in a.split(":")]
        h2, m2 = [int(x) for x in b.split(":")]
        if not (0 <= h1 < 24 and 0 <= h2 <= 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
            raise ValueError
        return h1 * 60 + m1, h2 * 60 + m2
    except (ValueError, TypeError):
        sys.exit("Sesión inválida '%s' (usa HH:MM-HH:MM, hora de Nueva York)" % text)


def in_sess(mins, sess):
    a, b = sess
    return (a <= mins < b) if a <= b else (mins >= a or mins < b)


KILLZONES = [
    ("Londres", *parse_session(os.getenv("KZ_LONDON", "02:00-05:00"))),
    ("Nueva York", *parse_session(os.getenv("KZ_NEWYORK", "08:00-11:00"))),
]

NEWS_ON = os.getenv("NEWS_ON", "1").strip() != "0"
NEWS_COUNTRIES = {x.strip().upper() for x in os.getenv("NEWS_COUNTRIES", "USD").split(",") if x.strip()}
NEWS_IMPACTS = {x.strip().lower() for x in os.getenv("NEWS_IMPACTS", "high").split(",") if x.strip()}
NEWS_BEFORE_MIN = float(os.getenv("NEWS_BEFORE_MIN", "60"))
NEWS_AFTER_MIN = float(os.getenv("NEWS_AFTER_MIN", "30"))
NEWS_URL = os.getenv("NEWS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
_NEWS = {"done": False, "events": None}

# ============================================================================
# Parámetros Pine V8 Apex Lab
# ============================================================================
LOOKBACK_EXT = 35
USE_PDH = True
USE_ASIA = True
ASIA_SESS = parse_session("20:00-24:00")
PD_LEN = 100
MIN_WICK = 0.40
MIN_RANGE_ATR = 0.55
COOLDOWN = 10
SPACE_ATR = 1.1
SPACE_BARS = 18
MIN_SCORE = 6
HTF_LEN = 50
EMA_LEN = 20
BB_LEN = 20
BB_MULT = 2.0
RSI_LEN = 14
RSI_SELL_MIN = 50.0
RSI_BUY_MAX = 50.0
VOL_MULT = 1.08
MIN_ATR_USD = 1.9
ANTI_IMP = True
IMP_ADX = 28.0
ADX_MIN = 16.0
ADX_MIN_H4 = 18.0
BLOCK_DEAD = True
DEAD_SESS = parse_session("17:00-20:00")
SL_BUFFER = 0.40
MIN_SL_ATR = 0.70
MAX_RISK_ATR = 3.2
USE_FVG_TRIG = env_bool("USE_FVG_TRIG", True)
USE_SQUEEZE = env_bool("USE_SQUEEZE", True)

BT_SPREAD = float(os.getenv("BACKTEST_SPREAD", "0.5"))
BT_EVAL_BARS = int(os.getenv("BACKTEST_EVAL_BARS", "55"))
BT_RR = os.getenv("BACKTEST_RR", "1:2")
BT_WARMUP = 150
RRS = [1.0, 1.5, 2.0, 3.0]
RR_NAMES = ["1:1", "1:1.5", "1:2", "1:3"]
if BT_RR not in RR_NAMES:
    sys.exit("BACKTEST_RR debe ser uno de %s" % ", ".join(RR_NAMES))
BRK_IDX = RR_NAMES.index(BT_RR)


def profile(tf):
    """Perfil temporal automático del Pine Script V8 Apex."""
    if 60 <= tf < 240:    # H1
        return dict(
            name="H1",
            htf="240",
            wick=MIN_WICK,
            cooldown=7,
            space_bars=12,
            space_atr=0.85,
            adx_min=13.0,
            vol=VOL_MULT,
        )
    if tf >= 240:         # H4 o superior
        return dict(
            name="H4+",
            htf="D",
            wick=MIN_WICK,
            cooldown=COOLDOWN,
            space_bars=SPACE_BARS,
            space_atr=SPACE_ATR,
            adx_min=ADX_MIN_H4,
            vol=VOL_MULT,
        )
    # M15 (base)
    return dict(
        name="base",
        htf="240",
        wick=MIN_WICK,
        cooldown=COOLDOWN,
        space_bars=SPACE_BARS,
        space_atr=SPACE_ATR,
        adx_min=ADX_MIN,
        vol=VOL_MULT,
    )


# ============================================================================
# Descarga de datos
# ============================================================================
def market_open(dt):
    loc = dt.astimezone(NY)
    wd = loc.weekday()
    if wd == 5:
        return False
    if wd == 4 and loc.hour >= 17:
        return False
    if wd == 6 and loc.hour < 17:
        return False
    return True


def wait_for_close(now):
    step = 15 * 60
    ts = now.timestamp()
    next_close = (math.floor(ts / step) + 1) * step
    last_close = next_close - step
    sleep_s = 0.0
    if next_close - ts <= MAX_WAIT_S:
        sleep_s = (next_close - ts) + CLOSE_MARGIN_S
        print(
            "Faltan %.0f s para el cierre de las %s UTC: espero %.0f s y reviso justo después."
            % (
                next_close - ts,
                datetime.fromtimestamp(next_close, timezone.utc).strftime("%H:%M"),
                sleep_s,
            )
        )
    elif ts - last_close < CLOSE_MARGIN_S:
        sleep_s = CLOSE_MARGIN_S - (ts - last_close)
        print("La vela acaba de cerrar: espero %.1f s para confirmación." % sleep_s)
    if sleep_s > 0:
        time.sleep(sleep_s)
        return datetime.now(timezone.utc)
    return now


def dedupe_sorted(bars):
    bars.sort(key=lambda b: b["t"])
    out = []
    for b in bars:
        if not out or b["t"] > out[-1]["t"]:
            out.append(b)
    return out


def parse_rows(rows):
    bars = []
    for v in rows:
        t = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        bars.append(
            dict(
                t=t,
                o=float(v["open"]),
                h=float(v["high"]),
                l=float(v["low"]),
                c=float(v["close"]),
                v=float(v.get("volume") or 0),
            )
        )
    return dedupe_sorted(bars)


def fetch_twelvedata(tf):
    key = os.getenv("TWELVEDATA_KEY")
    if not key:
        sys.exit("Falta el secret TWELVEDATA_KEY")
    params = dict(
        symbol=SYMBOL,
        interval=TF_INFO[tf]["td"],
        outputsize=OUTPUTSIZE,
        order="ASC",
        timezone="UTC",
        apikey=key,
    )
    last_err = None
    data = None
    for _ in range(3):
        try:
            r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=30)
            data = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            last_err = type(e).__name__
            time.sleep(5)
    if data is None or "values" not in data:
        sys.exit("Error Twelve Data: %s" % (data.get("message") if data else last_err))
    return parse_rows(data["values"])


def capital_session():
    global _CAPITAL_SESSION
    if _CAPITAL_SESSION:
        return _CAPITAL_SESSION
    key = os.getenv("CAPITAL_API_KEY")
    ident = os.getenv("CAPITAL_IDENTIFIER")
    pwd = os.getenv("CAPITAL_PASSWORD")
    if not (key and ident and pwd):
        sys.exit("Faltan secrets de Capital.com")
    pref = os.getenv("CAPITAL_ENV", "").strip().lower()
    order = ["live", "demo"] if pref == "live" else ["demo", "live"]
    last = "?"
    for env in order:
        base = CAPITAL_HOSTS[env]
        for _ in range(3):
            try:
                r = requests.post(
                    base + "/api/v1/session",
                    headers={"X-CAP-API-KEY": key, "Content-Type": "application/json"},
                    json={"identifier": ident, "password": pwd, "encryptedPassword": False},
                    timeout=30,
                )
            except requests.RequestException as e:
                last = "%s en %s" % (type(e).__name__, env)
                time.sleep(3)
                continue
            if r.status_code == 200 and r.headers.get("CST") and r.headers.get("X-SECURITY-TOKEN"):
                _CAPITAL_SESSION = (
                    base,
                    env,
                    {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]},
                )
                return _CAPITAL_SESSION
            last = "HTTP %d en %s: %s" % (r.status_code, env, r.text[:120])
            if r.status_code == 429:
                time.sleep(2)
                continue
            break
        time.sleep(1.2)
    sys.exit("Fallo en sesión de Capital.com (%s)" % last)


def capital_px(d):
    bid, ask = d.get("bid"), d.get("ask")
    if CAPITAL_PRICE == "bid":
        return float(bid if bid is not None else ask)
    if CAPITAL_PRICE == "ask":
        return float(ask if ask is not None else bid)
    return (float(bid) + float(ask)) / 2.0


def parse_capital(prices):
    bars = []
    for p in prices:
        try:
            t = datetime.strptime(p["snapshotTimeUTC"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            bars.append(
                dict(
                    t=t,
                    o=capital_px(p["openPrice"]),
                    h=capital_px(p["highPrice"]),
                    l=capital_px(p["lowPrice"]),
                    c=capital_px(p["closePrice"]),
                    v=float(p.get("lastTradedVolume") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return dedupe_sorted(bars)


def fetch_capital(tf, windows):
    global CAPITAL_ENV_USED
    base, env, hdr = capital_session()
    CAPITAL_ENV_USED = env
    info = TF_INFO[tf]
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S"
    rows = []
    for k in range(windows):
        t_to = now - timedelta(days=info["days"] * k)
        t_from = t_to - timedelta(days=info["days"])
        params = {"resolution": info["res"], "max": 1000, "from": t_from.strftime(fmt), "to": t_to.strftime(fmt)}
        data = None
        for _ in range(3):
            try:
                r = requests.get(
                    "%s/api/v1/prices/%s" % (base, CAPITAL_EPIC),
                    params=params,
                    headers=hdr,
                    timeout=30,
                )
                if r.status_code == 200:
                    data = r.json()
                    break
                if r.status_code == 429:
                    time.sleep(2)
                    continue
            except requests.RequestException:
                time.sleep(2)
                continue
        if data:
            rows.extend(data.get("prices") or [])
        time.sleep(0.25)
    bars = parse_capital(rows)
    if not bars:
        sys.exit("Capital.com respondió sin velas de %s." % info["name"])
    return bars


def fetch_bars(tf, extended=False):
    if DATA_SOURCE == "capital":
        w = max(TF_INFO[tf]["win"], WINDOWS_EXTENDED) if extended else TF_INFO[tf]["win"]
        return fetch_capital(tf, w)
    return fetch_twelvedata(tf)


def fetch_latest(tf):
    return fetch_capital(tf, 1) if DATA_SOURCE == "capital" else fetch_twelvedata(tf)


def source_label():
    if DATA_SOURCE == "capital":
        return "Capital.com %s (%s)" % (CAPITAL_ENV_USED or "?", CAPITAL_PRICE)
    return "Twelve Data"


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return parse_rows(list(csv.DictReader(f)))


def clean_bars(bars, now):
    return [b for b in bars if market_open(b["t"]) and b["t"] + BAR <= now]


# ============================================================================
# Indicadores técnicos (fieles a TradingView)
# ============================================================================
def rma(src, n, start=0):
    out = [None] * len(src)
    if len(src) < start + n:
        return out
    out[start + n - 1] = sum(src[start : start + n]) / n
    for i in range(start + n, len(src)):
        out[i] = (src[i] + (n - 1) * out[i - 1]) / n
    return out


def ema_seq(src, n):
    out = [None] * len(src)
    if len(src) < n:
        return out
    out[n - 1] = sum(src[:n]) / n
    a = 2.0 / (n + 1)
    for i in range(n, len(src)):
        out[i] = a * src[i] + (1 - a) * out[i - 1]
    return out


def sma_seq(src, n):
    out = [None] * len(src)
    for i in range(n - 1, len(src)):
        w = src[i - n + 1 : i + 1]
        if None in w:
            continue
        out[i] = sum(w) / n
    return out


def build_context(bars, htf):
    """Calcula HTF EMA50 y PDH/PDL con lookahead_on [1] (cierre confirmado anterior)."""
    n = len(bars)
    htf_cl = [None] * n
    htf_ema = [None] * n
    pdh = [None] * n
    pdl = [None] * n
    alpha = 2.0 / (HTF_LEN + 1)

    cur_k = None
    cur_close = None
    prev_close = None
    prev_ema = None
    closes = []
    cur_day = None
    cur_hi = cur_lo = None
    prev_hi = prev_lo = None

    for i, b in enumerate(bars):
        loc = b["t"].astimezone(NY) - timedelta(hours=17)
        k_day = (loc.year, loc.month, loc.day)
        k_h = k_day if htf == "D" else (loc.year, loc.month, loc.day, loc.hour // 4)

        if k_h != cur_k:
            if cur_k is not None:
                prev_close = cur_close
                closes.append(prev_close)
                if len(closes) == HTF_LEN:
                    prev_ema = sum(closes) / HTF_LEN
                elif len(closes) > HTF_LEN:
                    prev_ema = alpha * prev_close + (1 - alpha) * prev_ema
            cur_k = k_h
        cur_close = b["c"]
        htf_cl[i] = prev_close
        htf_ema[i] = prev_ema

        if k_day != cur_day:
            if cur_day is not None:
                prev_hi, prev_lo = cur_hi, cur_lo
            cur_day = k_day
            cur_hi, cur_lo = b["h"], b["l"]
        else:
            cur_hi = max(cur_hi, b["h"])
            cur_lo = min(cur_lo, b["l"])
        pdh[i] = prev_hi
        pdl[i] = prev_lo
    return htf_cl, htf_ema, pdh, pdl


# ============================================================================
# Motor de Señales V8 Apex Lab
# ============================================================================
def run_v8(bars):
    prof = profile(TF)
    n = len(bars)
    O = [b["o"] for b in bars]
    H = [b["h"] for b in bars]
    L = [b["l"] for b in bars]
    C = [b["c"] for b in bars]
    VOL = [b["v"] for b in bars]

    tr = [H[0] - L[0]] + [
        max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)
    ]
    atr = rma(tr, 14)

    pdm = [0.0]
    mdm = [0.0]
    for i in range(1, n):
        up = H[i] - H[i - 1]
        dn = L[i - 1] - L[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    trr = rma(tr, 14)
    pr = rma(pdm, 14)
    mr = rma(mdm, 14)
    di_p = [None] * n
    di_m = [None] * n
    dx = [0.0] * n
    for i in range(n):
        if trr[i]:
            di_p[i] = 100.0 * pr[i] / trr[i]
            di_m[i] = 100.0 * mr[i] / trr[i]
            s = di_p[i] + di_m[i]
            dx[i] = abs(di_p[i] - di_m[i]) / (s if s != 0 else 1.0)
    adx_raw = rma(dx, 14, start=13)
    adx = [None if v is None else 100.0 * v for v in adx_raw]

    ema_ref = ema_seq(C, EMA_LEN)
    bb_mid = sma_seq(C, BB_LEN)
    bb_up = [None] * n
    bb_lo = [None] * n
    bb_w = [None] * n
    for i in range(BB_LEN - 1, n):
        w = C[i - BB_LEN + 1 : i + 1]
        m = bb_mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / BB_LEN)
        bb_up[i] = m + BB_MULT * sd
        bb_lo[i] = m - BB_MULT * sd
        bb_w[i] = (bb_up[i] - bb_lo[i]) / m if m else None
    bb_w_avg = sma_seq(bb_w, 50)

    ups = [0.0] + [max(C[i] - C[i - 1], 0.0) for i in range(1, n)]
    dns = [0.0] + [max(C[i - 1] - C[i], 0.0) for i in range(1, n)]
    au, ad = rma(ups, RSI_LEN, start=1), rma(dns, RSI_LEN, start=1)
    rsi = [None] * n
    for i in range(n):
        if au[i] is not None:
            rsi[i] = 100.0 if ad[i] == 0 else 100.0 - 100.0 / (1.0 + au[i] / ad[i])

    vol_avg = sma_seq(VOL, 20)
    htf_cl, htf_ema, pdh, pdl = build_context(bars, prof["htf"])

    sigs = []
    last_bar = -1000
    last_px = None
    asia_hi = asia_lo = asia_cur_hi = asia_cur_lo = None
    prev_in_asia = False

    for i in range(n):
        o, h, l, c = O[i], H[i], L[i], C[i]
        ny = bars[i]["t"].astimezone(NY)
        mins = ny.hour * 60 + ny.minute

        in_asia = in_sess(mins, ASIA_SESS)
        if in_asia and not prev_in_asia:
            asia_cur_hi, asia_cur_lo = h, l
        elif in_asia:
            asia_cur_hi = h if asia_cur_hi is None else max(asia_cur_hi, h)
            asia_cur_lo = l if asia_cur_lo is None else min(asia_cur_lo, l)
        if (not in_asia) and prev_in_asia:
            asia_hi, asia_lo = asia_cur_hi, asia_cur_lo
        prev_in_asia = in_asia

        a = atr[i]
        if a is None:
            continue

        in_dead = in_sess(mins, DEAD_SESS)
        time_ok = not (BLOCK_DEAD and in_dead and TF < 240)

        hc, he = htf_cl[i], htf_ema[i]
        htf_bull = hc is not None and he is not None and hc > he
        htf_bear = hc is not None and he is not None and hc < he

        adx_i, dip, dim = adx[i], di_p[i], di_m[i]
        strong_up = adx_i is not None and dip is not None and adx_i >= IMP_ADX and dip > dim
        strong_dn = adx_i is not None and dip is not None and adx_i >= IMP_ADX and dim > dip
        adx_ok = prof["adx_min"] <= 0 or (adx_i is not None and adx_i >= prof["adx_min"])

        va = vol_avg[i]
        vol_spike = va is not None and va > 0 and VOL[i] >= prof["vol"] * va

        bw, bwa = bb_w[i], bb_w_avg[i]
        not_dead_sq = bw is not None and bwa is not None and bw >= 0.70 * bwa
        squeeze_ok = (not USE_SQUEEZE) or not_dead_sq

        pd_mid = None
        if i >= PD_LEN - 1:
            pd_mid = (max(H[i - PD_LEN + 1 : i + 1]) + min(L[i - PD_LEN + 1 : i + 1])) / 2.0

        rng = h - l
        rng_nz = rng if rng != 0 else 1.0
        wick_up = (h - max(o, c)) / rng_nz
        wick_dn = (min(o, c) - l) / rng_nz
        body_pct = abs(c - o) / rng_nz

        nb_high = max(H[i - LOOKBACK_EXT : i]) if i >= LOOKBACK_EXT else None
        nb_low = min(L[i - LOOKBACK_EXT : i]) if i >= LOOKBACK_EXT else None
        sw_nb_h = nb_high is not None and h > nb_high and c < nb_high
        sw_nb_l = nb_low is not None and l < nb_low and c > nb_low
        sw_pd_h = USE_PDH and pdh[i] is not None and h > pdh[i] and c < pdh[i]
        sw_pd_l = USE_PDH and pdl[i] is not None and l < pdl[i] and c > pdl[i]
        sw_as_h = USE_ASIA and asia_hi is not None and h > asia_hi and c < asia_hi
        sw_as_l = USE_ASIA and asia_lo is not None and l < asia_lo and c > asia_lo

        fvg_bear = i >= 2 and h >= L[i - 2] and c < L[i - 2] and O[i - 1] > C[i - 1]
        fvg_bull = i >= 2 and l <= H[i - 2] and c > H[i - 2] and C[i - 1] > O[i - 1]

        ema_i = ema_ref[i]
        rsi_i = rsi[i]

        ev_bear = (
            (1 if c < o else 0)
            + (1 if wick_up >= 0.42 else 0)
            + (1 if bb_up[i] is not None and h > bb_up[i] else 0)
            + (1 if rsi_i is not None and rsi_i >= RSI_SELL_MIN else 0)
            + (1 if ema_i is not None and (h - ema_i) >= 0 else 0)
            + (2 if htf_bear else 0)
            + (1 if pd_mid is not None and c >= pd_mid else 0)
            + (1 if vol_spike else 0)
            + (2 if (sw_pd_h or sw_as_h or sw_nb_h) else (1 if fvg_bear else 0))
        )

        ev_bull = (
            (1 if c > o else 0)
            + (1 if wick_dn >= 0.42 else 0)
            + (1 if bb_lo[i] is not None and l < bb_lo[i] else 0)
            + (1 if rsi_i is not None and rsi_i <= RSI_BUY_MAX else 0)
            + (1 if ema_i is not None and (ema_i - l) >= 0 else 0)
            + (2 if htf_bull else 0)
            + (1 if pd_mid is not None and c <= pd_mid else 0)
            + (1 if vol_spike else 0)
            + (2 if (sw_pd_l or sw_as_l or sw_nb_l) else (1 if fvg_bull else 0))
        )

        cool_ok = (i - last_bar) > prof["cooldown"]
        atr_ok = a >= MIN_ATR_USD
        test_ema_bear = ema_i is not None and h >= (ema_i - 0.25 * a)
        test_ema_bull = ema_i is not None and l <= (ema_i + 0.25 * a)
        space_sell = (
            last_px is None or c >= last_px + prof["space_atr"] * a or (i - last_bar) > prof["space_bars"]
        )
        space_buy = (
            last_px is None or c <= last_px - prof["space_atr"] * a or (i - last_bar) > prof["space_bars"]
        )

        bear_classic = wick_up >= prof["wick"] and body_pct >= 0.22 and rng >= MIN_RANGE_ATR * a and c < o
        bear_fvg = fvg_bear and wick_up >= (prof["wick"] - 0.08) and vol_spike and c < o
        bull_classic = wick_dn >= prof["wick"] and body_pct >= 0.22 and rng >= MIN_RANGE_ATR * a and c > o
        bull_fvg = fvg_bull and wick_dn >= (prof["wick"] - 0.08) and vol_spike and c > o

        sell_ready = (
            (bear_classic or (USE_FVG_TRIG and bear_fvg))
            and ev_bear >= MIN_SCORE
            and htf_bear
            and test_ema_bear
            and not (ANTI_IMP and strong_up)
            and space_sell
            and cool_ok
            and atr_ok
            and adx_ok
            and time_ok
            and squeeze_ok
        )
        buy_ready = (
            (bull_classic or (USE_FVG_TRIG and bull_fvg))
            and ev_bull >= MIN_SCORE
            and htf_bull
            and test_ema_bull
            and not (ANTI_IMP and strong_dn)
            and space_buy
            and cool_ok
            and atr_ok
            and adx_ok
            and time_ok
            and squeeze_ok
        )

        sl_s = h + SL_BUFFER * a
        if sl_s - c < MIN_SL_ATR * a:
            sl_s = c + MIN_SL_ATR * a
        risk_s = sl_s - c

        sl_b = l - SL_BUFFER * a
        if c - sl_b < MIN_SL_ATR * a:
            sl_b = c - MIN_SL_ATR * a
        risk_b = c - sl_b

        sell_sig = sell_ready and 0 < risk_s <= MAX_RISK_ATR * a
        buy_sig = buy_ready and 0 < risk_b <= MAX_RISK_ATR * a

        if sell_sig:
            last_bar, last_px = i, c
            sigs.append(
                dict(
                    side="VENTA",
                    i=i,
                    label_i=i,
                    close=c,
                    sl=sl_s,
                    risk=risk_s,
                    atr=a,
                    trig=0 if bear_classic else 1,
                    score=ev_bear,
                )
            )
        if buy_sig:
            last_bar, last_px = i, c
            sigs.append(
                dict(
                    side="COMPRA",
                    i=i,
                    label_i=i,
                    close=c,
                    sl=sl_b,
                    risk=risk_b,
                    atr=a,
                    trig=0 if bull_classic else 1,
                    score=ev_bull,
                )
            )
    return sigs


# ============================================================================
# Telegram / Noticias
# ============================================================================
def fmt_t(t):
    return t.astimezone(EC).strftime("%d/%m %H:%M")


def _hm(mins):
    return "%02d:%02d" % ((mins // 60) % 24, mins % 60)


def killzone_text(t_open):
    ny = t_open.astimezone(NY)
    mins = ny.hour * 60 + ny.minute
    day0 = datetime(ny.year, ny.month, ny.day, tzinfo=NY)

    def span(a, b):
        ea = (day0 + timedelta(minutes=a)).astimezone(EC).strftime("%H:%M")
        eb = (day0 + timedelta(minutes=b)).astimezone(EC).strftime("%H:%M")
        return "%s-%s Ecuador / %s-%s NY" % (ea, eb, _hm(a), _hm(b))

    for name, a, b in KILLZONES:
        if in_sess(mins, (a, b)):
            return "✅ Killzone: SÍ, dentro de %s (%s)" % (name, span(a, b)), name
    detalle = " y ".join("%s (%s)" % (name, span(a, b)) for name, a, b in KILLZONES)
    return "⚪ Killzone: NO, fuera de %s" % detalle, None


def parse_event_time(text):
    dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(timezone.utc)


def fetch_news():
    if _NEWS["done"]:
        return _NEWS["events"]
    _NEWS["done"] = True
    for _ in range(2):
        try:
            r = requests.get(NEWS_URL, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                data = r.json()
                events = []
                for e in data:
                    country = str(e.get("country") or e.get("currency") or "").upper()
                    impact = str(e.get("impact", "")).lower()
                    if country in NEWS_COUNTRIES and impact in NEWS_IMPACTS:
                        events.append((parse_event_time(e["date"]), str(e.get("title", "?")), country))
                _NEWS["events"] = events
                return events
        except Exception:
            time.sleep(1)
    _NEWS["events"] = None
    return None


def news_lines(now):
    if not NEWS_ON:
        return []
    events = fetch_news()
    if events is None:
        return ["📰 Calendario no disponible: verifica noticias rojas de USD antes de operar."]
    by_time = {}
    for dt, title, country in events:
        delta = (dt - now).total_seconds() / 60.0
        if -NEWS_AFTER_MIN <= delta <= NEWS_BEFORE_MIN:
            by_time.setdefault(dt, []).append(title)
    out = []
    for dt in sorted(by_time):
        delta = (dt - now).total_seconds() / 60.0
        cuando = "en %d min" % round(delta) if delta >= 0 else "salió hace %d min" % round(-delta)
        out.append(
            "⚠️ NOTICIA ROJA USD %s (%s Ecuador): %s."
            % (cuando, dt.astimezone(EC).strftime("%H:%M"), ", ".join(by_time[dt]))
        )
    return out


def build_message(s, bars, now=None):
    now = now or datetime.now(timezone.utc)
    b = bars[s["i"]]
    close_time = b["t"] + BAR
    side = s["side"]
    d = 1 if side == "COMPRA" else -1
    icon = "🟢" if side == "COMPRA" else "🔴"
    trig = "Rechazo clásico" if s["trig"] == 0 else "Mitigación FVG"
    lines = [
        "<b>%s %s XAU/USD %s  [V8 Apex Lab]</b>" % (icon, side, TF_INFO[TF]["label"].upper()),
        "• Patrón: %s | Puntuación: %d/11" % (trig, s["score"]),
        "• <b>Entrada:</b> <code>%.2f</code> (apertura de vela siguiente)" % s["close"],
        "• <b>Stop Loss:</b> <code>%.2f</code> (Riesgo: $%.2f)" % (s["sl"], s["risk"]),
        "• <b>TP 1:1:</b> <code>%.2f</code> (Mover a BE)" % (s["close"] + d * 1.0 * s["risk"]),
        "• <b>TP 1:2:</b> <code>%.2f</code> (Objetivo principal)" % (s["close"] + d * 2.0 * s["risk"]),
        "• <b>TP 1:3:</b> <code>%.2f</code> (Runner)" % (s["close"] + d * 3.0 * s["risk"]),
        "• Vela cerrada: %s (Hora Ecuador)" % fmt_t(close_time),
    ]
    try:
        lines.append(killzone_text(b["t"])[0])
    except Exception:
        pass
    try:
        lines.extend(news_lines(now))
    except Exception:
        pass
    lines.append("\n<i>Cero repintado • Ejecutar en apertura inmediata</i>")
    return "\n".join(lines)


def send_telegram(text):
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("Faltan secrets TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=25,
        )
        return r.status_code == 200
    except Exception as e:
        print("Error Telegram: %s" % e)
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": []}


def save_state(state):
    state["sent"] = state["sent"][-150:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def state_key(side, t):
    stamp = t.strftime("%Y-%m-%dT%H:%M")
    return "%s|%s|%s" % (side, tf_name(), stamp)


# ============================================================================
# Modos diagnósticos (history, backtest, capital)
# ============================================================================
def bt_measure(bars, sigs):
    n = len(bars)
    out = []
    for s in sigs:
        d = 1 if s["side"] == "COMPRA" else -1
        row = []
        for rr in RRS:
            tgt = s["close"] + d * rr * s["risk"]
            st = None
            for k in range(s["i"] + 1, n):
                bb = bars[k]
                stop = bb["l"] <= s["sl"] if d == 1 else bb["h"] >= s["sl"]
                hit = bb["h"] >= tgt if d == 1 else bb["l"] <= tgt
                if stop:
                    st = "L"
                    break
                if hit:
                    st = "W"
                    break
                if k >= s["i"] + BT_EVAL_BARS:
                    st = "E"
                    break
            row.append(st)
        out.append(row)
    return out


def pct(w, l):
    return "%.1f%%" % (100.0 * w / (w + l)) if w + l else "-"


def exp_r(sr, n):
    return "%+.2f" % (sr / n) if n else "-"


def run_backtest():
    print("MEDIDOR V8 Apex Lab (M15, H1, H4) idéntico a Pine Script.")
    for tf in TIMEFRAMES:
        set_tf(tf)
        try:
            raw = fetch_bars(tf, True)
        except SystemExit as e:
            print("\n[%s] ERROR: %s" % (tf_name(), e.code))
            continue
        bars = clean_bars(raw, datetime.now(timezone.utc))
        if len(bars) < 250:
            print("\n[%s] Muy pocas velas (%d)." % (tf_name(), len(bars)))
            continue
        sigs = [s for s in run_v8(bars) if s["i"] >= BT_WARMUP]
        res = bt_measure(bars, sigs)
        weeks = max(1.0, (bars[-1]["t"] - bars[BT_WARMUP]["t"]).total_seconds() / 604800.0)
        print("\n=== %s (perfil %s) | %d velas | %s -> %s (hora Ecuador) ==="
              % (tf_name(), profile(TF)["name"], len(bars), fmt_t(bars[0]["t"]), fmt_t(bars[-1]["t"])))
        nb = sum(1 for s in sigs if s["side"] == "COMPRA")
        print("Señales: %d (compra %d, venta %d) | ~%.1f/sem" % (len(sigs), nb, len(sigs) - nb, len(sigs) / weeks))
        print("Ratio | Aciertos | Fallos | Precisión | Gana/op (R)")
        for r, rr in enumerate(RRS):
            w = l = 0
            sr = 0.0
            for s, row in zip(sigs, res):
                cost = BT_SPREAD / s["risk"]
                if row[r] == "W":
                    w += 1
                    sr += rr - cost
                elif row[r] == "L":
                    l += 1
                    sr -= 1.0 + cost
            print("%-5s | %8d | %6d | %9s | %s" % (RR_NAMES[r], w, l, pct(w, l), exp_r(sr, w + l)))


# ============================================================================
# Ejecución Normal
# ============================================================================
def expected_last_open(now):
    step = TF * 60
    return datetime.fromtimestamp((now.timestamp() // step) * step - step, timezone.utc)


def check_timeframe(tf, now, state, raw=None):
    set_tf(tf)
    name = tf_name()
    live = raw is None
    if live:
        raw = fetch_bars(tf, False)
    else:
        now = raw[-1]["t"] + timedelta(days=1)
    bars = clean_bars(raw, now)

    if live:
        exp = expected_last_open(now)
        for _ in range(FRESH_RETRIES):
            ny_h = exp.astimezone(NY).hour
            if not market_open(exp) or ny_h == 17 or (bars and bars[-1]["t"] >= exp):
                break
            time.sleep(3)
            try:
                raw = dedupe_sorted(raw + fetch_latest(tf))
            except SystemExit:
                break
            now = datetime.now(timezone.utc)
            bars = clean_bars(raw, now)

    if len(bars) < 200:
        print("[%s] Pocas velas (%d). Saltando." % (name, len(bars)))
        return False

    sigs = run_v8(bars)
    n = len(bars)
    fresh = [s for s in sigs if s["i"] >= n - LOOKBACK_BARS]
    changed = False

    for s in fresh:
        key = state_key(s["side"], bars[s["i"]]["t"])
        if key in state["sent"]:
            continue
        age = (now - (bars[s["i"]]["t"] + BAR)).total_seconds() / 60.0
        if age > MAX_AGE_MIN:
            continue
        if send_telegram(build_message(s, bars, now)):
            state["sent"].append(key)
            changed = True
            print("[%s] Aviso enviado: %s" % (name, key))
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="normal", choices=["normal", "test", "history", "backtest", "capital"])
    ap.add_argument("--tf", type=int, default=15, choices=sorted(TF_INFO))
    ap.add_argument("--csv", help="(pruebas) leer velas de un CSV")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)

    if args.mode == "test":
        ok = send_telegram("✅ <b>Bot Oro V8 Apex (M15 / H1 / H4) Conectado</b>\nTelegram funcionando.")
        print("Mensaje de prueba enviado" if ok else "Fallo al enviar")
        return

    if args.mode == "backtest":
        run_backtest()
        return

    if args.mode == "capital":
        set_tf(args.tf)
        raw = fetch_capital(args.tf, 1)
        print(f"Conexión exitosa a Capital.com ({CAPITAL_ENV_USED}). Velas {tf_name()}: {len(raw)}")
        for b in clean_bars(raw, now)[-5:]:
            print(f"{fmt_t(b['t'])} | O:{b['o']:.2f} H:{b['h']:.2f} L:{b['l']:.2f} C:{b['c']:.2f}")
        return

    if args.mode == "normal":
        if not args.csv:
            now = wait_for_close(now)
            if not market_open(now - timedelta(minutes=1)):
                print("Mercado cerrado.")
                return
        state = load_state()
        for tf in TIMEFRAMES:
            try:
                if check_timeframe(tf, now, state):
                    save_state(state)
            except Exception as e:
                print(f"[{TF_INFO[tf]['name']}] Error: {e}")


if __name__ == "__main__":
    main()
