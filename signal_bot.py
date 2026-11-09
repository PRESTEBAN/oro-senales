#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oro SMC V8 Apex Lab -> avisos por Telegram en M15, H1 y H4.
Corre en GitHub Actions, disparado desde cron-job.org.

ESTE BOT ES UN PORT 1:1 DEL INDICADOR PINE "Oro Sniper SMC - Alpha Green V8 Apex Lab":
  * Cero retraso: la senal se genera en la MISMA vela que produce el rechazo (vela 1). Sin esperar
    velas posteriores para confirmar. Entrada = cierre de la vela 1 (= apertura de la vela 2).
  * Cero repintado: solo se evaluan velas YA CERRADAS (equivale a barstate.isconfirmed). Una vez
    avisada, la senal no se mueve ni se borra.
  * SL protegido al extremo: ventas -> high + 0.40 x ATR ; compras -> low - 0.40 x ATR
    (con el piso de 0.70 x ATR y el riesgo maximo de 3.2 x ATR del Pine).
  * Alerta instantanea al cierre: el bot se despierta justo antes del cierre de la vela, espera
    al cierre (+ unos segundos de margen) y manda el aviso en ese instante.

Perfil temporal automatico (igual que el Pine):
  M15 -> "Frecuente" (mecha 0.35, cooldown 6, espacio 10 velas / 1.0 ATR, puntuacion minima 5, ADX min 16, HTF 4H)
  H1  -> (mecha 0.40, cooldown 7, espacio 12 velas / 0.85 ATR, puntuacion 6, ADX min 13, HTF 4H)
  H4  -> (mecha 0.40, cooldown 10, espacio 18 velas / 1.1 ATR, puntuacion 6, ADX min 18, HTF DIARIO)
Filtro anti-spread: riesgo minimo de $3.50 (evita microvelas donde el spread destruye el 1:1).

Killzone y noticias rojas de USD SOLO se informan en el mensaje; nunca filtran senales.

Modos:  python signal_bot.py --mode normal    (el de siempre: revisa M15, H1 y H4)
        python signal_bot.py --mode test      (manda un mensaje de prueba)
        python signal_bot.py --mode capital   [--tf 15|60|240]  (prueba la conexion e imprime velas)
        python signal_bot.py --mode noticias  (noticias rojas de USD de la semana)
        python signal_bot.py --mode backtest  (medidor identico al del Pine: 1:1, 1:1.5, 1:2, 1:3,
                                               desglose, mitades, IC95 y punto de equilibrio)
        python signal_bot.py --mode history   [--tf 15|60|240]  (senales del historial descargado)
        python signal_bot.py --mode velas     [--tf 15|60|240]  (ultimas velas con sus marcas)
Secrets: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
         CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD   (si usas Capital.com)
         TWELVEDATA_KEY                                          (si usas Twelve Data)
El bot SOLO LEE precios: nunca abre ordenes ni toca tu cuenta.
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================================
# Configuracion general
# ============================================================================
NY = ZoneInfo("America/New_York")
EC = ZoneInfo("America/Guayaquil")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# 'days' = tamano de cada peticion a Capital.com (max 1000 velas). 'win' = tramos en modo normal.
# M30 necesita mas historia porque su filtro de tendencia es la EMA50 DIARIA.
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
            sys.exit("TIMEFRAMES solo admite 15, 60 y 240 (separados por coma). Recibi: %s" % text)
        if int(x) not in out:
            out.append(int(x))
    if not out:
        sys.exit("TIMEFRAMES esta vacio")
    return out


def env_bool(name, default):
    return os.getenv(name, "1" if default else "0").strip() not in ("0", "false", "False", "")


TIMEFRAMES = parse_timeframes(os.getenv("TIMEFRAMES", "15,60,240"))
DATA_SOURCE = os.getenv("DATA_SOURCE", "twelvedata").strip().lower()   # capital | twelvedata
SYMBOL = os.getenv("SYMBOL", "XAU/USD")
OUTPUTSIZE = int(os.getenv("OUTPUTSIZE", "5000"))
CAPITAL_EPIC = os.getenv("CAPITAL_EPIC", "GOLD")
CAPITAL_PRICE = os.getenv("CAPITAL_PRICE", "bid").strip().lower()      # bid | mid | ask
CAPITAL_HOSTS = {"demo": "https://demo-api-capital.backend-capital.com",
                 "live": "https://api-capital.backend-capital.com"}
CAPITAL_ENV_USED = ""
_CAPITAL_SESSION = None
WINDOWS_EXTENDED = int(os.getenv("WINDOWS_EXTENDED", "6"))

if DATA_SOURCE not in ("capital", "twelvedata"):
    sys.exit("DATA_SOURCE debe ser capital o twelvedata")
if CAPITAL_PRICE not in ("bid", "mid", "ask"):
    sys.exit("CAPITAL_PRICE debe ser bid, mid o ask")

# Cero retraso: solo se avisa la vela que ACABA de cerrar (LOOKBACK 1) y, si el aviso se atrasa mas de
# MAX_AGE_MIN minutos (cola de GitHub, feed lento), se descarta: la entrada ya no seria la apertura de la vela 2.
LOOKBACK_BARS = int(os.getenv("LOOKBACK_BARS", "2"))     # se miran las 2 ultimas velas; el filtro de edad decide
MAX_AGE_MIN = float(os.getenv("MAX_AGE_MIN", "10"))      # M15
# H1 y H4 toleran mas retraso: si la corrida de justo-al-cierre no vio la vela, la siguiente (15 min despues) la rescata.
MAX_AGE_BY_TF = {15: MAX_AGE_MIN, 60: float(os.getenv("MAX_AGE_H1", "20")), 240: float(os.getenv("MAX_AGE_H4", "30"))}
CLOSE_MARGIN_S = float(os.getenv("CLOSE_MARGIN_S", "3"))
MAX_WAIT_S = float(os.getenv("MAX_WAIT_S", "100"))
FRESH_RETRIES = int(os.getenv("FRESH_RETRIES", "3"))     # reintentos si la vela recien cerrada aun no llega del feed


def parse_session(text):
    """'HH:MM-HH:MM' -> (minuto de inicio, minuto de fin) del dia."""
    try:
        a, b = text.replace(" ", "").split("-")
        h1, m1 = [int(x) for x in a.split(":")]
        h2, m2 = [int(x) for x in b.split(":")]
        if not (0 <= h1 < 24 and 0 <= h2 <= 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
            raise ValueError
        return h1 * 60 + m1, h2 * 60 + m2
    except (ValueError, TypeError):
        sys.exit("Sesion invalida '%s' (usa HH:MM-HH:MM, hora de Nueva York)" % text)


def in_sess(mins, sess):
    a, b = sess
    return (a <= mins < b) if a <= b else (mins >= a or mins < b)


KILLZONES = [("Londres", *parse_session(os.getenv("KZ_LONDON", "02:00-05:00"))),
             ("Nueva York", *parse_session(os.getenv("KZ_NEWYORK", "08:00-11:00")))]

NEWS_ON = os.getenv("NEWS_ON", "1").strip() != "0"
NEWS_COUNTRIES = {x.strip().upper() for x in os.getenv("NEWS_COUNTRIES", "USD").split(",") if x.strip()}
NEWS_IMPACTS = {x.strip().lower() for x in os.getenv("NEWS_IMPACTS", "high").split(",") if x.strip()}
NEWS_BEFORE_MIN = float(os.getenv("NEWS_BEFORE_MIN", "60"))
NEWS_AFTER_MIN = float(os.getenv("NEWS_AFTER_MIN", "30"))
NEWS_URL = os.getenv("NEWS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
_NEWS = {"done": False, "events": None}

# ============================================================================
# Parametros del Pine V8 Apex Lab (mismos valores por defecto que los inputs)
# ============================================================================
LOOKBACK_EXT = 35          # lookbackExt
USE_PDH = True             # usePDH
USE_ASIA = True            # useAsia
ASIA_SESS = parse_session("20:00-24:00")    # "2000-0000" (hora NY)
PD_LEN = 100               # pdLen
MIN_WICK = 0.40            # minWick
MIN_RANGE_ATR = 0.55       # minRangeATR
COOLDOWN = 10              # cooldown (base)
SPACE_ATR = 1.1            # spaceATR (base)
SPACE_BARS = 18            # spaceBars (base)
MIN_SCORE = 6              # minScore
HTF_LEN = 50               # htfLen
EMA_LEN = 20               # emaLen
BB_LEN = 20
BB_MULT = 2.0
RSI_LEN = 14
RSI_SELL_MIN = 50.0
RSI_BUY_MAX = 50.0
VOL_MULT = 1.08
MIN_ATR_USD = 1.9          # minAtrUSD
ANTI_IMP = True            # antiImp
IMP_ADX = 28.0             # impAdx
ADX_MIN = 16.0             # adxMin (base)
ADX_MIN_H4 = 18.0          # adxMinH4 (no se usa en M15/M30/H1)
BLOCK_DEAD = True          # blockDead
DEAD_SESS = parse_session("17:00-20:00")    # horas muertas (hora NY)
SL_BUFFER = 0.40           # slBuffer
MIN_SL_ATR = 0.70          # minSlATR
MAX_RISK_ATR = 3.2         # maxRiskATR
MIN_RISK_USD = 3.50        # minRiskUSD (anti-spread)
USE_FVG_TRIG = env_bool("USE_FVG_TRIG", True)     # interruptor del Lab
USE_SQUEEZE = env_bool("USE_SQUEEZE", True)       # interruptor del Lab

# Medidor (backtest)
BT_SPREAD = float(os.getenv("BACKTEST_SPREAD", "0.5"))     # spreadUSD
BT_EVAL_BARS = int(os.getenv("BACKTEST_EVAL_BARS", "55"))  # evalBars
BT_RR = os.getenv("BACKTEST_RR", "1:2")                    # rrBreak
BT_WARMUP = 250
RRS = [1.0, 1.5, 2.0, 3.0]
RR_NAMES = ["1:1", "1:1.5", "1:2", "1:3"]
if BT_RR not in RR_NAMES:
    sys.exit("BACKTEST_RR debe ser uno de %s" % ", ".join(RR_NAMES))
BRK_IDX = RR_NAMES.index(BT_RR)


def profile(tf):
    """Perfil temporal automatico del Pine (seccion 7)."""
    if tf == 15:      # isM15
        return dict(name="M15 Frecuente", htf="240", wick=0.35, cooldown=6, space_bars=10, space_atr=1.0,
                    min_score=5, adx_min=ADX_MIN, vol=VOL_MULT)
    if 60 <= tf < 240:   # isH1_H3
        return dict(name="H1", htf="240", wick=MIN_WICK, cooldown=7, space_bars=12, space_atr=0.85,
                    min_score=MIN_SCORE, adx_min=13.0, vol=VOL_MULT)
    return dict(name="H4+", htf="D", wick=MIN_WICK, cooldown=COOLDOWN, space_bars=SPACE_BARS,
                space_atr=SPACE_ATR, min_score=MIN_SCORE, adx_min=ADX_MIN_H4, vol=VOL_MULT)


# ============================================================================
# Mercado / datos
# ============================================================================
def market_open(dt):
    """Oro: cierra viernes 17:00 NY y reabre domingo 17:00 NY."""
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
    """Si el cierre de una vela de 15 min esta cerca (o acaba de pasar), espera hasta cierre + margen.
    Todos los cierres de M30 y H1 caen en cierres de M15, asi que basta la rejilla de 15 minutos."""
    step = 15 * 60
    ts = now.timestamp()
    next_close = (math.floor(ts / step) + 1) * step
    last_close = next_close - step
    sleep_s = 0.0
    if next_close - ts <= MAX_WAIT_S:
        sleep_s = (next_close - ts) + CLOSE_MARGIN_S
        print("Faltan %.0f s para el cierre de las %s UTC: espero %.0f s y reviso justo despues."
              % (next_close - ts, datetime.fromtimestamp(next_close, timezone.utc).strftime("%H:%M"), sleep_s))
    elif ts - last_close < CLOSE_MARGIN_S:
        sleep_s = CLOSE_MARGIN_S - (ts - last_close)
        print("La vela acaba de cerrar: espero %.1f s para que quede final." % sleep_s)
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
        bars.append(dict(t=t, o=float(v["open"]), h=float(v["high"]), l=float(v["low"]),
                         c=float(v["close"]), v=float(v.get("volume") or 0)))
    return dedupe_sorted(bars)


def fetch_twelvedata(tf):
    key = os.getenv("TWELVEDATA_KEY")
    if not key:
        sys.exit("Falta el secret TWELVEDATA_KEY")
    params = dict(symbol=SYMBOL, interval=TF_INFO[tf]["td"], outputsize=OUTPUTSIZE, order="ASC",
                  timezone="UTC", apikey=key)
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
    if data is None:
        sys.exit("No se pudo consultar Twelve Data (%s)" % last_err)
    if data.get("status") == "error" or "values" not in data:
        sys.exit("Twelve Data respondio con error: %s" % data.get("message"))
    return parse_rows(data["values"])


def capital_session():
    global _CAPITAL_SESSION
    if _CAPITAL_SESSION:
        return _CAPITAL_SESSION
    key = os.getenv("CAPITAL_API_KEY")
    ident = os.getenv("CAPITAL_IDENTIFIER")
    pwd = os.getenv("CAPITAL_PASSWORD")
    if not (key and ident and pwd):
        sys.exit("Faltan los secrets CAPITAL_API_KEY, CAPITAL_IDENTIFIER y/o CAPITAL_PASSWORD")
    pref = os.getenv("CAPITAL_ENV", "").strip().lower()
    order = ["live", "demo"] if pref == "live" else ["demo", "live"]
    last = "?"
    for env in order:
        base = CAPITAL_HOSTS[env]
        for _ in range(3):
            try:
                r = requests.post(base + "/api/v1/session",
                                  headers={"X-CAP-API-KEY": key, "Content-Type": "application/json"},
                                  json={"identifier": ident, "password": pwd, "encryptedPassword": False},
                                  timeout=30)
            except requests.RequestException as e:
                last = "%s en %s" % (type(e).__name__, env)
                time.sleep(3)
                continue
            if r.status_code == 200 and r.headers.get("CST") and r.headers.get("X-SECURITY-TOKEN"):
                _CAPITAL_SESSION = (base, env, {"CST": r.headers["CST"],
                                                "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]})
                return _CAPITAL_SESSION
            last = "HTTP %d en %s: %s" % (r.status_code, env, r.text[:120])
            if r.status_code == 429:
                time.sleep(2)
                continue
            break
        time.sleep(1.2)
    sys.exit("No se pudo abrir sesion en Capital.com (%s). Revisa los 3 secrets de Capital." % last)


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
            bars.append(dict(t=t, o=capital_px(p["openPrice"]), h=capital_px(p["highPrice"]),
                             l=capital_px(p["lowPrice"]), c=capital_px(p["closePrice"]),
                             v=float(p.get("lastTradedVolume") or 0)))
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
        err = "?"
        for _ in range(3):
            try:
                r = requests.get("%s/api/v1/prices/%s" % (base, CAPITAL_EPIC), params=params,
                                 headers=hdr, timeout=30)
            except requests.RequestException as e:
                err = type(e).__name__
                time.sleep(3)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    err = "respuesta no es JSON"
                    continue
                break
            err = "HTTP %d: %s" % (r.status_code, r.text[:150])
            if r.status_code == 429:
                time.sleep(2)
                continue
            break
        if data is None:
            sys.exit("Capital.com no entrego velas de %s (%s)" % (info["name"], err))
        prices = data.get("prices") or []
        if len(prices) >= 1000:
            print("AVISO: un tramo de %s trajo 1000 velas (el maximo); podria faltar historial." % info["name"])
        rows.extend(prices)
        if k < windows - 1:
            time.sleep(0.3)
    bars = parse_capital(rows)
    if not bars:
        sys.exit("Capital.com respondio sin velas de %s. Revisa CAPITAL_EPIC (ahora: %s)."
                 % (info["name"], CAPITAL_EPIC))
    return bars


def fetch_bars(tf, extended=False):
    if DATA_SOURCE == "capital":
        w = max(TF_INFO[tf]["win"], WINDOWS_EXTENDED) if extended else TF_INFO[tf]["win"]
        return fetch_capital(tf, w)
    return fetch_twelvedata(tf)


def fetch_latest(tf):
    """Descarga rapida (solo el tramo mas reciente) para reintentar si la ultima vela aun no llego."""
    return fetch_capital(tf, 1) if DATA_SOURCE == "capital" else fetch_twelvedata(tf)


def source_label():
    if DATA_SOURCE == "capital":
        return "Capital.com %s (%s)" % (CAPITAL_ENV_USED or "?", CAPITAL_PRICE)
    return "Twelve Data"


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return parse_rows(list(csv.DictReader(f)))


def clean_bars(bars, now):
    """Quita velas con mercado cerrado y la vela que aun no cierra (nunca se evalua una vela abierta)."""
    return [b for b in bars if market_open(b["t"]) and b["t"] + BAR <= now]


# ============================================================================
# Indicadores base (equivalentes a Pine)
# ============================================================================
def rma(src, n, start=0):
    out = [None] * len(src)
    if len(src) < start + n:
        return out
    out[start + n - 1] = sum(src[start:start + n]) / n
    for i in range(start + n, len(src)):
        out[i] = (src[i] + (n - 1) * out[i - 1]) / n
    return out


def ema_seq(src, n):
    """ta.ema de Pine: semilla = SMA de las primeras n velas."""
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
        w = src[i - n + 1:i + 1]
        if None in w:
            continue
        out[i] = sum(w) / n
    return out


def build_context(bars, htf):
    """Equivalente a los request.security del Pine (lookahead_on con [1] = ultima barra HTF CERRADA):
       - htf_cl / htf_ema: cierre y EMA50 de la ultima vela HTF cerrada (HTF = '240' o 'D', dia 17:00 NY)
       - pdh / pdl: maximo y minimo del dia anterior."""
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
            if cur_k is not None:          # la vela HTF anterior acaba de cerrar
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
# MOTOR V8 APEX LAB (port 1:1 del Pine)
# ============================================================================
def run_v8(bars):
    """Devuelve la lista de senales. Cada senal nace en la vela i (la del rechazo), con entrada = cierre
    de esa vela y SL al extremo. No mira ninguna vela posterior."""
    prof = profile(TF)
    n = len(bars)
    O = [b["o"] for b in bars]
    H = [b["h"] for b in bars]
    L = [b["l"] for b in bars]
    C = [b["c"] for b in bars]
    VOL = [b["v"] for b in bars]

    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1])) for i in range(1, n)]
    atr = rma(tr, 14)

    # ---- ta.dmi(14, 14) ----
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

    # ---- EMA20, Bollinger, RSI, volumen ----
    ema_ref = ema_seq(C, EMA_LEN)
    bb_mid = sma_seq(C, BB_LEN)
    bb_up = [None] * n
    bb_lo = [None] * n
    bb_w = [None] * n
    for i in range(BB_LEN - 1, n):
        w = C[i - BB_LEN + 1:i + 1]
        m = bb_mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / BB_LEN)      # stdev poblacional, como Pine
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

        # ---- sesion Asia (rango de la ultima sesion completa) ----
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
        time_ok = not (BLOCK_DEAD and in_dead and TF < 240)          # tfSec < 14400 siempre se cumple en M15/M30/H1

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
            pd_mid = (max(H[i - PD_LEN + 1:i + 1]) + min(L[i - PD_LEN + 1:i + 1])) / 2.0

        rng = h - l
        rng_nz = rng if rng != 0 else 1.0
        wick_up = (h - max(o, c)) / rng_nz
        wick_dn = (min(o, c) - l) / rng_nz
        body_pct = abs(c - o) / rng_nz

        nb_high = max(H[i - LOOKBACK_EXT:i]) if i >= LOOKBACK_EXT else None
        nb_low = min(L[i - LOOKBACK_EXT:i]) if i >= LOOKBACK_EXT else None
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
        # ---- puntuacion bajista ----
        ev_bear = ((1 if c < o else 0)
                   + (1 if wick_up >= 0.42 else 0)
                   + (1 if bb_up[i] is not None and h > bb_up[i] else 0)
                   + (1 if rsi_i is not None and rsi_i >= RSI_SELL_MIN else 0)
                   + (1 if ema_i is not None and (h - ema_i) >= 0 else 0)
                   + (2 if htf_bear else 0)
                   + (1 if pd_mid is not None and c >= pd_mid else 0)
                   + (1 if vol_spike else 0)
                   + (2 if (sw_pd_h or sw_as_h or sw_nb_h) else (1 if fvg_bear else 0)))
        # ---- puntuacion alcista ----
        ev_bull = ((1 if c > o else 0)
                   + (1 if wick_dn >= 0.42 else 0)
                   + (1 if bb_lo[i] is not None and l < bb_lo[i] else 0)
                   + (1 if rsi_i is not None and rsi_i <= RSI_BUY_MAX else 0)
                   + (1 if ema_i is not None and (ema_i - l) >= 0 else 0)
                   + (2 if htf_bull else 0)
                   + (1 if pd_mid is not None and c <= pd_mid else 0)
                   + (1 if vol_spike else 0)
                   + (2 if (sw_pd_l or sw_as_l or sw_nb_l) else (1 if fvg_bull else 0)))

        cool_ok = (i - last_bar) > prof["cooldown"]
        atr_ok = a >= MIN_ATR_USD
        test_ema_bear = ema_i is not None and h >= (ema_i - 0.25 * a)
        test_ema_bull = ema_i is not None and l <= (ema_i + 0.25 * a)
        space_sell = last_px is None or c >= last_px + prof["space_atr"] * a or (i - last_bar) > prof["space_bars"]
        space_buy = last_px is None or c <= last_px - prof["space_atr"] * a or (i - last_bar) > prof["space_bars"]

        # ---- gatillo dual: rechazo clasico O mitigacion FVG ----
        bear_classic = (wick_up >= prof["wick"] and body_pct >= 0.22 and rng >= MIN_RANGE_ATR * a and c < o)
        bear_fvg = fvg_bear and wick_up >= (prof["wick"] - 0.08) and vol_spike and c < o
        bull_classic = (wick_dn >= prof["wick"] and body_pct >= 0.22 and rng >= MIN_RANGE_ATR * a and c > o)
        bull_fvg = fvg_bull and wick_dn >= (prof["wick"] - 0.08) and vol_spike and c > o

        sell_ready = ((bear_classic or (USE_FVG_TRIG and bear_fvg)) and ev_bear >= prof["min_score"] and htf_bear
                      and test_ema_bear and not (ANTI_IMP and strong_up) and space_sell and cool_ok
                      and atr_ok and adx_ok and time_ok and squeeze_ok)
        buy_ready = ((bull_classic or (USE_FVG_TRIG and bull_fvg)) and ev_bull >= prof["min_score"] and htf_bull
                     and test_ema_bull and not (ANTI_IMP and strong_dn) and space_buy and cool_ok
                     and atr_ok and adx_ok and time_ok and squeeze_ok)

        # ---- SL al extremo de la vela del rechazo ----
        sl_s = h + SL_BUFFER * a
        if sl_s - c < MIN_SL_ATR * a:
            sl_s = c + MIN_SL_ATR * a
        risk_s = sl_s - c
        sl_b = l - SL_BUFFER * a
        if c - sl_b < MIN_SL_ATR * a:
            sl_b = c - MIN_SL_ATR * a
        risk_b = c - sl_b

        sell_sig = sell_ready and 0 < risk_s <= MAX_RISK_ATR * a and risk_s >= MIN_RISK_USD
        buy_sig = buy_ready and 0 < risk_b <= MAX_RISK_ATR * a and risk_b >= MIN_RISK_USD

        if sell_sig:
            last_bar, last_px = i, c
            sigs.append(dict(side="VENTA", i=i, label_i=i, close=c, sl=sl_s, risk=risk_s, atr=a,
                             trig=0 if bear_classic else 1, score=ev_bear))
        if buy_sig:
            last_bar, last_px = i, c
            sigs.append(dict(side="COMPRA", i=i, label_i=i, close=c, sl=sl_b, risk=risk_b, atr=a,
                             trig=0 if bull_classic else 1, score=ev_bull))
    return sigs


# ============================================================================
# Telegram / estado / mensajes
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
            return "\u2705 Killzone: SI, dentro de %s (%s)" % (name, span(a, b)), name
    detalle = " y ".join("%s (%s)" % (name, span(a, b)) for name, a, b in KILLZONES)
    return "\u26AA Killzone: NO, fuera de %s" % detalle, None


def parse_event_time(text):
    dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(timezone.utc)


def fetch_news():
    if _NEWS["done"]:
        return _NEWS["events"]
    _NEWS["done"] = True
    err = "?"
    for _ in range(2):
        try:
            r = requests.get(NEWS_URL, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as e:
            err = type(e).__name__
            time.sleep(1)
            continue
        if r.status_code != 200:
            err = "HTTP %d" % r.status_code
            if r.status_code == 429:
                break
            time.sleep(1)
            continue
        try:
            data = r.json()
        except ValueError:
            err = "la respuesta no es JSON"
            break
        if not isinstance(data, list):
            err = "formato inesperado"
            break
        events = []
        for e in data:
            try:
                country = str(e.get("country") or e.get("currency") or "").upper()
                impact = str(e.get("impact", "")).lower()
                if country in NEWS_COUNTRIES and impact in NEWS_IMPACTS:
                    events.append((parse_event_time(e["date"]), str(e.get("title", "?")), country))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
        _NEWS["events"] = events
        print("Calendario economico: %d eventos %s (impacto %s) esta semana."
              % (len(events), "/".join(sorted(NEWS_COUNTRIES)), "/".join(sorted(NEWS_IMPACTS))))
        return events
    _NEWS["events"] = None
    print("Calendario economico NO disponible (%s)." % err)
    return None


def news_lines(now):
    if not NEWS_ON:
        return []
    events = fetch_news()
    paises = "/".join(sorted(NEWS_COUNTRIES))
    if events is None:
        return ["\U0001F4F0 Calendario economico no disponible: revisa si hay noticias rojas de %s antes de entrar." % paises]
    by_time = {}
    for dt, title, country in events:
        delta = (dt - now).total_seconds() / 60.0
        if -NEWS_AFTER_MIN <= delta <= NEWS_BEFORE_MIN:
            by_time.setdefault(dt, []).append(title)
    out = []
    for dt in sorted(by_time):
        delta = (dt - now).total_seconds() / 60.0
        cuando = "en %d min" % round(delta) if delta >= 0 else "salio hace %d min" % round(-delta)
        out.append("\u26A0\uFE0F NOTICIA ROJA %s %s (%s Ecuador / %s NY): %s. "
                   "Volatilidad fuerte: cuidado con la entrada y el SL."
                   % (paises, cuando, dt.astimezone(EC).strftime("%H:%M"), dt.astimezone(NY).strftime("%H:%M"),
                      ", ".join(by_time[dt])))
    return out


_DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]


def run_news_check():
    now = datetime.now(timezone.utc)
    print("Ahora: %s hora Ecuador / %s hora NY" % (now.astimezone(EC).strftime("%d/%m %H:%M"),
                                                   now.astimezone(NY).strftime("%d/%m %H:%M")))
    events = fetch_news()
    if events is None:
        print("No se pudo consultar el calendario (mira el mensaje de arriba).")
        return
    if not events:
        print("El calendario no trae eventos con ese filtro esta semana.")
        return
    print("  dia   hora Ecuador | hora NY | evento")
    for dt, title, country in sorted(events):
        e, ny = dt.astimezone(EC), dt.astimezone(NY)
        print("  %s %s   %s | %s | %s %s" % (_DIAS[e.weekday()], e.strftime("%d/%m"), e.strftime("%H:%M"),
                                              ny.strftime("%H:%M"), country, title))
    lines = news_lines(now)
    print("\nLo que diria un aviso enviado AHORA:")
    print("  " + ("(nada: no hay noticias rojas cerca)" if not lines else "\n  ".join(lines)))


def footer():
    if DATA_SOURCE == "capital":
        return ("Precios de Capital.com (%s), el mismo feed de tu grafico con fuente Capital.com. "
                "Confirma en tu grafico antes de operar." % CAPITAL_PRICE)
    return ("Precios del feed Twelve Data: pueden diferir unos dolares de TradingView. "
            "Confirma en tu grafico antes de operar.")


def build_message(s, bars, now=None):
    now = now or datetime.now(timezone.utc)
    b = bars[s["i"]]
    close_time = b["t"] + BAR
    side = s["side"]
    d = 1 if side == "COMPRA" else -1
    icon = "\U0001F7E2" if side == "COMPRA" else "\U0001F534"
    trig = "Rechazo clasico" if s["trig"] == 0 else "Mitigacion FVG"
    lines = ["%s %s XAU/USD %s  [V8 Apex Lab]" % (icon, side, TF_INFO[TF]["label"]),
             "Disparo: %s | Puntuacion %d/11" % (trig, s["score"]),
             "Entrada: %.2f (cierre de la vela de la senal) -> ejecuta a mercado en la APERTURA de la vela siguiente"
             % s["close"],
             "SL: %.2f (riesgo $%.2f)" % (s["sl"], s["risk"]),
             "TP 1:1 %.2f | 1:1.5 %.2f | 1:2 %.2f | 1:3 %.2f"
             % tuple(s["close"] + d * rr * s["risk"] for rr in RRS),
             "Vela %s cerrada: %s (hora Ecuador)" % (tf_name(), fmt_t(close_time))]
    delay = (now - close_time).total_seconds()
    if delay > 120:
        lines.append("\u23F1 Aviso con %d min de retraso tras el cierre: revisa que el precio no se haya alejado de la entrada."
                     % round(delay / 60.0))
    try:
        lines.append(killzone_text(b["t"])[0])
    except Exception as e:
        print("AVISO: no se pudo calcular la killzone (%s); el aviso sale igual." % type(e).__name__)
    try:
        lines.extend(news_lines(now))
    except Exception as e:
        print("AVISO: no se pudo revisar las noticias (%s); el aviso sale igual." % type(e).__name__)
    lines.append("")
    lines.append(footer())
    return "\n".join(lines)


def send_telegram(text):
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("Faltan los secrets TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    try:
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % token,
                          json={"chat_id": chat, "text": text}, timeout=30)
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            print("Telegram rechazo el mensaje: HTTP %s %s" % (r.status_code, r.text[:200]))
        return bool(ok)
    except (requests.RequestException, ValueError) as e:
        print("Error enviando a Telegram: %s" % type(e).__name__)
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data.get("sent"), list):
                return data
    except (OSError, ValueError):
        pass
    return {"sent": []}


def save_state(state):
    state["sent"] = state["sent"][-150:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
        f.write("\n")


def state_key(side, t):
    stamp = t.strftime("%Y-%m-%dT%H:%M")
    if TF == 15:
        return "%s|%s" % (side, stamp)
    return "%s|%s|%s" % (side, tf_name(), stamp)


# ============================================================================
# Diagnostico: conexion, history, velas
# ============================================================================
def run_capital_check():
    if DATA_SOURCE != "capital":
        sys.exit("Este modo prueba Capital.com: pon DATA_SOURCE=capital")
    raw = fetch_capital(TF, 1)
    now = datetime.now(timezone.utc)
    print("Sesion abierta en Capital.com (%s) | epic %s | precio %s | temporalidad %s | velas recibidas: %d"
          % (CAPITAL_ENV_USED, CAPITAL_EPIC, CAPITAL_PRICE, tf_name(), len(raw)))
    print("Primera vela: %s | ultima vela recibida: %s (hora Ecuador)" % (fmt_t(raw[0]["t"]), fmt_t(raw[-1]["t"])))
    print("Ahora: %s (hora Ecuador)" % fmt_t(now))
    nvol = sum(1 for b in raw if b["v"] > 0)
    print("Velas con volumen > 0: %d de %d (el Pine usa volumen: sin volumen no hay disparo FVG ni punto de volumen)"
          % (nvol, len(raw)))
    bars = clean_bars(raw, now)
    if not bars:
        print("No hay velas cerradas con mercado abierto (fin de semana?).")
        return
    print("\nUltimas 12 velas %s cerradas, hora de APERTURA (hora Ecuador, igual que TradingView):" % tf_name())
    print("hora         |   apertura    maximo     minimo     cierre     volumen")
    for b in bars[-12:]:
        print("%s | %10.2f %10.2f %10.2f %10.2f %10.0f" % (fmt_t(b["t"]), b["o"], b["h"], b["l"], b["c"], b["v"]))
    print("\nCompara 2 o 3 de estas velas con las de tu grafico (pasa el mouse sobre la vela en TradingView).")


def print_history(sigs, bars):
    print("Senales V8 en el historial (%d). Comparalas con las etiquetas de TradingView:" % len(sigs))
    print("(ignora las de los primeros dias: los indicadores necesitan velas para calentarse)")
    for s in sigs:
        kz = killzone_text(bars[s["i"]]["t"])[1] or "fuera"
        print("%-6s %-15s p%-2d | vela %s | cierre %.2f SL %.2f (riesgo $%.2f) | KZ: %s"
              % (s["side"], "clasico" if s["trig"] == 0 else "FVG", s["score"], fmt_t(bars[s["i"]]["t"]),
                 s["close"], s["sl"], s["risk"], kz))


# ============================================================================
# Medidor (identico al del Pine) / backtest
# ============================================================================
def bt_measure(bars, sigs):
    """Mismas reglas que el medidor del Pine: la evaluacion empieza en la vela SIGUIENTE a la senal; en cada
    vela se mira primero el SL (si toca SL y objetivo a la vez, cuenta SL); expira a las evalBars velas.
    Resultado por senal y por ratio: 'W', 'L', 'E' (expirada) o None (aun abierta)."""
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


def wilson(w, n):
    nn = float(max(n, 1))
    p = w / nn
    den = 1.0 + 3.8416 / nn
    cen = (p + 3.8416 / (2.0 * nn)) / den
    hlf = 1.96 * math.sqrt(p * (1.0 - p) / nn + 3.8416 / (4.0 * nn * nn)) / den
    return cen - hlf, cen + hlf


def run_backtest():
    print("MEDIDOR V8 Apex Lab con las velas reales que baja el bot (misma mecánica que la tabla del Pine).")
    print("Entrada = cierre de la vela de la senal. Spread $%.2f descontado en R. Evaluacion maxima %d velas. "
          "Si una vela toca SL y objetivo a la vez, cuenta SL." % (BT_SPREAD, BT_EVAL_BARS))
    print("Interruptores: FVG %s | compresion %s" % ("on" if USE_FVG_TRIG else "off", "on" if USE_SQUEEZE else "off"))
    ok_count = 0
    for tf in TIMEFRAMES:
        set_tf(tf)
        try:
            raw = fetch_bars(tf, True)
        except SystemExit as e:
            print("\n[%s] ERROR: %s" % (tf_name(), e.code))
            continue
        bars = clean_bars(raw, datetime.now(timezone.utc))
        if len(bars) < 600:
            print("\n[%s] Muy pocas velas utiles (%d)." % (tf_name(), len(bars)))
            continue
        ok_count += 1
        sigs = [s for s in run_v8(bars) if s["i"] >= BT_WARMUP]
        res = bt_measure(bars, sigs)
        weeks = max(1.0, (bars[-1]["t"] - bars[BT_WARMUP]["t"]).total_seconds() / 604800.0)
        print("\n=== %s (perfil %s) | %d velas | %s -> %s (hora Ecuador) | fuente %s ==="
              % (tf_name(), profile(TF)["name"], len(bars), fmt_t(bars[0]["t"]), fmt_t(bars[-1]["t"]), source_label()))
        nb = sum(1 for s in sigs if s["side"] == "COMPRA")
        print("Senales: %d (compra %d, venta %d) | ~%.1f/sem" % (len(sigs), nb, len(sigs) - nb, len(sigs) / weeks))
        print("Ratio | Aciertos | Fallos | Precision | Gana/op (R)")
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

        # ---- desglose y estabilidad al ratio elegido ----
        rr = RRS[BRK_IDX]
        recs = []   # (tiempo, 'W'/'L', R, senal)
        for s, row in zip(sigs, res):
            if row[BRK_IDX] in ("W", "L"):
                cost = BT_SPREAD / s["risk"]
                recs.append((s["i"], row[BRK_IDX], (rr - cost) if row[BRK_IDX] == "W" else (-1.0 - cost), s))
        print("Desglose y estabilidad (ratio %s)" % BT_RR)
        groups = [("Disparo clasico", lambda s: s["trig"] == 0), ("Disparo solo FVG", lambda s: s["trig"] == 1),
                  ("Compras", lambda s: s["side"] == "COMPRA"), ("Ventas", lambda s: s["side"] == "VENTA")]
        for nm, f in groups:
            sub = [x for x in recs if f(x[3])]
            gw = sum(1 for x in sub if x[1] == "W")
            gl = len(sub) - gw
            print("  %-17s resueltas %3d | precision %6s | gana/op %s" % (nm, len(sub), pct(gw, gl),
                                                                       exp_r(sum(x[2] for x in sub), len(sub))))
        if len(recs) >= 4:
            ts = sorted(x[0] for x in recs)
            t_med = (ts[len(ts) // 2] + ts[(len(ts) - 1) // 2]) / 2.0
            h1 = [x for x in recs if x[0] <= t_med]
            h2 = [x for x in recs if x[0] > t_med]
            ok = []
            for nm, sub in (("1a mitad", h1), ("2a mitad", h2)):
                w_ = sum(1 for x in sub if x[1] == "W")
                s_ = sum(x[2] for x in sub)
                ok.append(len(sub) > 0 and s_ > 0)
                print("  %-17s resueltas %3d | precision %6s | gana/op %s" % (nm, len(sub), pct(w_, len(sub) - w_),
                                                                           exp_r(s_, len(sub))))
            w_all = sum(1 for x in recs if x[1] == "W")
            p_all = w_all / float(len(recs))
            lo, hi = wilson(w_all, len(recs))
            c_avg = sum(BT_SPREAD / x[3]["risk"] for x in recs) / len(recs)
            p_be = (1.0 + c_avg) / (rr + 1.0)
            print("  Equilibrio %.1f%% | Acierto %.1f%% (IC95 %.0f-%.0f%%)" % (100 * p_be, 100 * p_all, 100 * lo, 100 * hi))
            verdict = ("Ventaja demostrada (IC95 sobre el equilibrio)" if lo > p_be else
                       ("Gana, pero la muestra aun no lo demuestra" if p_all > p_be else "Sin ventaja"))
            print("  %s | %s" % (verdict, "mitades estables" if all(ok) else "mitades inestables"))
        else:
            print("  Aun no hay suficientes senales resueltas.")
    if ok_count == 0:
        sys.exit("No se pudo medir ninguna temporalidad.")


# ============================================================================
# Revision de una temporalidad (modo normal)
# ============================================================================
def expected_last_open(now):
    step = TF * 60
    return datetime.fromtimestamp((now.timestamp() // step) * step - step, timezone.utc)


def next_bar_missing(bars, now):
    """True si, segun la cadencia real del feed (ultima vela + duracion), la vela que acaba de cerrar
    todavia no llego. Funciona igual en M15, H1 y H4 aunque Capital alinee H4 distinto a la rejilla UTC.
    Solo cuenta en los 20 min posteriores al cierre esperado (despues ya seria otro motivo: pausa, fin de semana)."""
    if not bars:
        return False
    nxt = bars[-1]["t"] + BAR                       # apertura de la vela siguiente
    close_exp = nxt + BAR                           # su cierre esperado
    if now < close_exp or now > close_exp + timedelta(minutes=20):
        return False
    return market_open(nxt) and nxt.astimezone(NY).hour != 17


def notify_error(state, errors):
    """Avisa por Telegram si una temporalidad no se pudo revisar (maximo 1 aviso por hora)."""
    last = state.get("err_ts")
    if last and time.time() - last < 3600:
        return
    text = "\u26A0\uFE0F El bot no pudo revisar: " + " | ".join(errors)[:600] + "\nLas demas temporalidades si se revisaron. Mira el log de GitHub Actions."
    try:
        if send_telegram(text):
            state["err_ts"] = time.time()
    except SystemExit:
        pass


def check_timeframe(tf, now, state, raw=None):
    """Revisa una temporalidad y manda el aviso de la vela que ACABA de cerrar. True si hubo avisos."""
    set_tf(tf)
    name = tf_name()
    live = raw is None
    if live:
        raw = fetch_bars(tf, False)
    else:
        now = raw[-1]["t"] + timedelta(days=1)     # (pruebas con CSV)
    bars = clean_bars(raw, now)

    # Cero retraso: si la vela recien cerrada todavia no llego del feed, reintenta unos segundos.
    if live:
        retries = FRESH_RETRIES if TF < 240 else FRESH_RETRIES * 3     # H4: el feed tarda mas en cerrar la vela
        for _ in range(retries):
            if not next_bar_missing(bars, now):
                break
            print("[%s] La vela que acaba de cerrar aun no llega del feed; reintento en 3 s." % name)
            time.sleep(3)
            try:
                raw = dedupe_sorted(raw + fetch_latest(tf))
            except SystemExit as e:
                print("[%s] reintento fallo: %s" % (name, e.code))
                break
            now = datetime.now(timezone.utc)
            bars = clean_bars(raw, now)

    if len(bars) < 300:
        sys.exit("Muy pocas velas utiles (%d) en %s. Revisa el feed." % (len(bars), name))
    last_close_time = bars[-1]["t"] + BAR
    age_last = (now - last_close_time).total_seconds() / 60.0
    print("[%s] Fuente: %s | perfil %s | velas: %d | ultima cerrada: %s UTC | ahora: %s UTC | atraso del feed: %.1f min"
          % (name, source_label(), profile(TF)["name"], len(bars), last_close_time.strftime("%Y-%m-%d %H:%M"),
             now.strftime("%Y-%m-%d %H:%M"), age_last))

    sigs = run_v8(bars)
    n = len(bars)
    if sigs:
        ls = sigs[-1]
        print("[%s] Ultima senal del historial: %s %s (vela cerrada %s hora Ecuador, hace %.0f min)"
              % (name, ls["side"], "clasico" if ls["trig"] == 0 else "FVG", fmt_t(bars[ls["i"]]["t"] + BAR),
                 (now - (bars[ls["i"]]["t"] + BAR)).total_seconds() / 60.0))
    else:
        print("[%s] El historial descargado no tiene ninguna senal (revisa que haya historia suficiente)." % name)
    fresh = [s for s in sigs if s["i"] >= n - LOOKBACK_BARS]
    changed = False
    if not fresh:
        print("[%s] Sin senales en la ultima vela cerrada." % name)
    for s in fresh:
        key = state_key(s["side"], bars[s["i"]]["t"])
        if key in state["sent"]:
            print("[%s] Ya avisada: %s" % (name, key))
            continue
        age = (now - (bars[s["i"]]["t"] + BAR)).total_seconds() / 60.0
        if age > MAX_AGE_BY_TF[tf]:
            print("[%s] Senal %s demasiado vieja (%.1f min): la entrada ya no seria la apertura de la vela siguiente."
                  % (name, key, age))
            continue
        if send_telegram(build_message(s, bars, now)):
            state["sent"].append(key)
            changed = True
            print("[%s] Aviso enviado: %s (%.1f min despues del cierre de la vela)" % (name, key, age))
    return changed


# ============================================================================
# Main
# ============================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="normal",
                    choices=["normal", "test", "history", "velas", "capital", "noticias", "backtest"])
    ap.add_argument("--tf", type=int, default=15, choices=sorted(TF_INFO),
                    help="temporalidad en minutos para capital / history / velas (15, 60 o 240)")
    ap.add_argument("--csv", help="(pruebas) leer velas de un CSV en vez de la API")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)

    if args.mode == "test":
        ok = send_telegram("\u2705 Bot de senales V8 Apex Lab conectado. Si ves este mensaje, Telegram funciona.")
        print("Mensaje de prueba enviado" if ok else "No se pudo enviar el mensaje de prueba")
        sys.exit(0 if ok else 1)
    if args.mode == "capital":
        set_tf(args.tf)
        run_capital_check()
        return
    if args.mode == "noticias":
        run_news_check()
        return
    if args.mode == "backtest":
        run_backtest()
        return

    if args.mode == "normal":
        if not args.csv:
            now = wait_for_close(now)
            if not market_open(now - timedelta(minutes=1)):
                print("Mercado cerrado, nada que revisar.")
                return
        state = load_state()
        if args.csv:
            if check_timeframe(args.tf, now, state, raw=load_csv(args.csv)):
                save_state(state)
            return
        ok_count = 0
        errors = []
        for tf in TIMEFRAMES:
            try:
                if check_timeframe(tf, now, state):
                    save_state(state)
                ok_count += 1
            except (SystemExit, Exception) as e:
                if isinstance(e, SystemExit):
                    msg = str(e.code) if e.code is not None else "error"
                else:
                    msg = "%s: %s" % (type(e).__name__, e)
                    traceback.print_exc()
                print("[%s] ERROR: %s" % (TF_INFO[tf]["name"], msg))
                errors.append("%s: %s" % (TF_INFO[tf]["name"], msg))
        if errors:
            notify_error(state, errors)
            save_state(state)
        if ok_count == 0:
            sys.exit("Ninguna temporalidad pudo revisarse. " + " | ".join(errors))
        if errors:
            print("AVISO: fallaron estas temporalidades (las demas se revisaron): " + " | ".join(errors))
        return

    # ---- history / velas: una sola temporalidad ----
    set_tf(args.tf)
    raw = load_csv(args.csv) if args.csv else fetch_bars(args.tf, True)
    if args.csv and raw:
        now = raw[-1]["t"] + timedelta(days=1)
    bars = clean_bars(raw, now)
    if len(bars) < 300:
        sys.exit("Muy pocas velas utiles (%d). Revisa el feed." % len(bars))
    print("[%s] Fuente: %s | perfil %s | velas: %d | ultima cerrada: %s UTC"
          % (tf_name(), source_label(), profile(TF)["name"], len(bars),
             (bars[-1]["t"] + BAR).strftime("%Y-%m-%d %H:%M")))
    sigs = run_v8(bars)

    if args.mode == "history":
        print_history(sigs, bars)
        return

    marks = {}
    for s in sigs:
        marks.setdefault(s["i"], []).append("%s %s" % (s["side"], "clasico" if s["trig"] == 0 else "FVG"))
    last = bars[-150:]
    first_i = len(bars) - len(last)
    print("Ultimas %d velas %s, hora de apertura = hora Ecuador (UTC-5):" % (len(last), tf_name()))
    print("hora         |   apertura    maximo     minimo     cierre | marcas")
    for k, b in enumerate(last):
        print("%s | %10.2f %10.2f %10.2f %10.2f | %s"
              % (fmt_t(b["t"]), b["o"], b["h"], b["l"], b["c"], ", ".join(marks.get(first_i + k, []))))


if __name__ == "__main__":
    main()
