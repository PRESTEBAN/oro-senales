#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oro SMC v8 + v9 -> avisos por Telegram en M15, M30 y H1.
Corre en GitHub Actions, disparado desde cron-job.org. Cada ejecucion, para cada temporalidad:
  1) baja las velas de XAU/USD de Capital.com (o de Twelve Data),
  2) repasa TODA la logica de los indicadores v8 y v9 vela por vela,
  3) si una de las ultimas 2 velas cerradas disparo COMPRA/VENTA (en v8, en v9
     o en ambos) y no se aviso antes, manda UN mensaje por Telegram.

AVISO LO MAS RAPIDO POSIBLE: si la ejecucion arranca poco antes del cierre de una vela de 15 min
(por ejemplo disparada en el minuto :14, :29, :44 o :59), el bot ESPERA al cierre + unos segundos
de margen y revisa en ese instante. Asi el tiempo de arranque de GitHub no cuenta en el retraso.

Temporalidades (variable TIMEFRAMES, por defecto "15,30,60"): M15, M30 y H1.
Tu Pine usa la misma temporalidad superior (4H) en M15, M30 y H1, asi que el motor es el mismo.
Cada aviso trae ademas: si la vela cerro dentro de una killzone (Londres 02-05 / Nueva York 08-11,
hora de NY, como tu Pine) y una advertencia si hay una NOTICIA ROJA (impacto alto) de USD cerca.
Ninguna de las dos cosas filtra senales: solo informan.
Fuente de datos (variable DATA_SOURCE): capital | twelvedata
Con Capital.com se usa el precio BID: es el que dibuja TradingView en CAPITALCOM:GOLD.

Modos:  python signal_bot.py --mode normal    (el de siempre: revisa todas las temporalidades)
        python signal_bot.py --mode test      (manda un mensaje de prueba)
        python signal_bot.py --mode capital   [--tf 15|30|60]  (prueba la conexion e imprime velas)
        python signal_bot.py --mode noticias  (muestra las noticias rojas de USD de la semana; no manda nada)
        python signal_bot.py --mode history   [--tf 15|30|60]  (senales del historial descargado)
        python signal_bot.py --mode velas     [--tf 15|30|60]  (ultimas 150 velas con sus marcas)
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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================================
# Configuracion
# ============================================================================
NY = ZoneInfo("America/New_York")
EC = ZoneInfo("America/Guayaquil")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Temporalidades soportadas. 'days' = tamano de cada peticion a Capital.com (maximo 1000 velas).
TF_INFO = {
    15: dict(name="M15", label="15m", res="MINUTE_15", td="15min", days=10),
    30: dict(name="M30", label="30m", res="MINUTE_30", td="30min", days=20),
    60: dict(name="H1", label="1h", res="HOUR", td="1h", days=40),
}
BAR = timedelta(minutes=15)    # duracion de la vela en uso; la cambia set_tf()
TF = 15


def set_tf(minutes):
    """Cambia la temporalidad con la que trabajan clean_bars, los mensajes y los modos."""
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
            sys.exit("TIMEFRAMES solo admite 15, 30 y 60 (separados por coma). Recibi: %s" % text)
        if int(x) not in out:
            out.append(int(x))
    if not out:
        sys.exit("TIMEFRAMES esta vacio")
    return out


TIMEFRAMES = parse_timeframes(os.getenv("TIMEFRAMES", "15,30,60"))

DATA_SOURCE = os.getenv("DATA_SOURCE", "twelvedata").strip().lower()   # capital | twelvedata
SYMBOL = os.getenv("SYMBOL", "XAU/USD")                  # solo Twelve Data
OUTPUTSIZE = int(os.getenv("OUTPUTSIZE", "3000"))        # solo Twelve Data
CAPITAL_EPIC = os.getenv("CAPITAL_EPIC", "GOLD")
CAPITAL_PRICE = os.getenv("CAPITAL_PRICE", "bid").strip().lower()   # bid | mid | ask
CAPITAL_HOSTS = {"demo": "https://demo-api-capital.backend-capital.com",
                 "live": "https://api-capital.backend-capital.com"}
CAPITAL_ENV_USED = ""
_CAPITAL_SESSION = None    # (base, entorno, cabeceras): se abre una sola vez por ejecucion
WINDOWS_NORMAL = 2         # tramos que se piden en el modo normal (por temporalidad)
WINDOWS_EXTENDED = 5       # tramos en los modos history / velas

FREQ = os.getenv("FREQ", "Equilibrado")  # Estricto | Equilibrado | Alto
LOOKBACK_BARS = 2    # avisa si la senal ocurrio en una de las ultimas N velas cerradas
MAX_AGE_MIN = 45     # no avisa senales mas viejas que esto (datos atrasados / reinicios)

# Espera al cierre de la vela (modo normal)
CLOSE_MARGIN_S = float(os.getenv("CLOSE_MARGIN_S", "3"))   # segundos tras el cierre, para que la vela quede final
MAX_WAIT_S = float(os.getenv("MAX_WAIT_S", "100"))         # solo espera si el cierre esta a menos de esto


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


# Killzones: SOLO se marcan en el aviso, nunca filtran senales. Mismas horas que tu Pine (hora de Nueva York).
KILLZONES = [("Londres", *parse_session(os.getenv("KZ_LONDON", "02:00-05:00"))),
             ("Nueva York", *parse_session(os.getenv("KZ_NEWYORK", "08:00-11:00")))]

# Eventos macro: SOLO se advierte en el aviso, nunca se bloquea nada. Fuente: calendario publico de Forex Factory.
NEWS_ON = os.getenv("NEWS_ON", "1").strip() != "0"
NEWS_COUNTRIES = {x.strip().upper() for x in os.getenv("NEWS_COUNTRIES", "USD").split(",") if x.strip()}
NEWS_IMPACTS = {x.strip().lower() for x in os.getenv("NEWS_IMPACTS", "high").split(",") if x.strip()}
NEWS_BEFORE_MIN = float(os.getenv("NEWS_BEFORE_MIN", "60"))   # avisa si el evento es en los proximos N min
NEWS_AFTER_MIN = float(os.getenv("NEWS_AFTER_MIN", "30"))     # ...o si salio hace menos de N min
NEWS_URL = os.getenv("NEWS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
_NEWS = {"done": False, "events": None}

if DATA_SOURCE not in ("capital", "twelvedata"):
    sys.exit("DATA_SOURCE debe ser capital o twelvedata")
if CAPITAL_PRICE not in ("bid", "mid", "ask"):
    sys.exit("CAPITAL_PRICE debe ser bid, mid o ask")

PRESETS = {
    "Estricto": dict(swLen=5, intLen=3, mssWait=25, setupWait=30, chochWait=8, microLen=3,
                     cooldown=6, obExtra=3, minLegATR=1.2, dispATR=0.6, fibA=0.5, fibB=0.786,
                     microBodyATR=0.3, maxRiskATR=1.5),
    "Equilibrado": dict(swLen=4, intLen=2, mssWait=30, setupWait=40, chochWait=10, microLen=2,
                        cooldown=4, obExtra=4, minLegATR=1.0, dispATR=0.45, fibA=0.382, fibB=0.786,
                        microBodyATR=0.25, maxRiskATR=1.8),
    "Alto": dict(swLen=3, intLen=2, mssWait=40, setupWait=50, chochWait=12, microLen=2,
                 cooldown=3, obExtra=5, minLegATR=0.8, dispATR=0.35, fibA=0.382, fibB=0.85,
                 microBodyATR=0.2, maxRiskATR=2.2),
}
if FREQ not in PRESETS:
    sys.exit("FREQ debe ser Estricto, Equilibrado o Alto")
PR = PRESETS[FREQ]

FVG_MIN_ATR = 0.1
REQ_POI = True
REQ_BOTH = False
SL_BUF = 0.15
MIN_RISK_ATR = 0.15
MAX_RISK_PX = 0.0
VP_LEN = 150
VP_ROWS = 24
USE_VP = True
USE_HTF = True
REQ_HTF = False
HTF_LEN = 50
PD_LEN = 100
MIN_SCORE = 2
RR = 2.0
USE_CONT = True
CONT_ON = USE_CONT and FREQ != "Estricto"
INT_SWEEP_ON = FREQ == "Alto"
CONT_MIN_SCORE = 3
CONT_MAX_LEG = 30
CONT_FIB_A = 0.236
CONT_MIN_LEG_ATR = 0.7
CONT_REARM = True
CONT_ADX = 20.0
ADX_LEN = 14


# ============================================================================
# Utilidades de mercado / datos
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
    """Si el cierre de una vela de 15 min esta cerca (o acaba de pasar), espera hasta
    cierre + CLOSE_MARGIN_S y devuelve la hora actual. Todos los cierres de M30 y H1
    caen en cierres de M15, asi que basta con mirar la rejilla de 15 minutos."""
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
    """Filas de Twelve Data / CSV: datetime, open, high, low, close (UTC)."""
    bars = []
    for v in rows:
        t = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        bars.append(dict(t=t, o=float(v["open"]), h=float(v["high"]), l=float(v["low"]),
                         c=float(v["close"]), v=float(v.get("volume") or 0)))
    return dedupe_sorted(bars)


# ---------------- Twelve Data ----------------
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


# ---------------- Capital.com ----------------
def capital_session():
    """Abre sesion (dura 10 min de inactividad). Se abre UNA vez por ejecucion y se reutiliza
    en las tres temporalidades. Devuelve (base, entorno, cabeceras)."""
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
                time.sleep(2)   # limite: 1 sesion por segundo
                continue
            break
        time.sleep(1.2)   # entre un entorno y el otro (limite: 1 sesion por segundo)
    sys.exit("No se pudo abrir sesion en Capital.com (%s). Revisa los 3 secrets de Capital." % last)


def capital_px(d):
    """Elige bid/ask/medio de un precio {'bid':..,'ask':..}."""
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
    """Baja 'windows' tramos (el limite de la API es 1000 velas por peticion) de la temporalidad tf."""
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
        time.sleep(0.3)
    bars = parse_capital(rows)
    if not bars:
        sys.exit("Capital.com respondio sin velas de %s. Revisa CAPITAL_EPIC (ahora: %s)."
                 % (info["name"], CAPITAL_EPIC))
    return bars


def fetch_bars(tf, extended=False):
    if DATA_SOURCE == "capital":
        return fetch_capital(tf, WINDOWS_EXTENDED if extended else WINDOWS_NORMAL)
    return fetch_twelvedata(tf)


def source_label():
    if DATA_SOURCE == "capital":
        return "Capital.com %s (%s)" % (CAPITAL_ENV_USED or "?", CAPITAL_PRICE)
    return "Twelve Data"


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return parse_rows(list(csv.DictReader(f)))


def clean_bars(bars, now):
    """Quita velas con mercado cerrado y la vela que aun no cierra (segun la temporalidad en uso)."""
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


def build_context(bars):
    """Sesgo 4H (EMA50 de la vela 4H previa), PDH/PDL del dia previo (dia 17:00 NY)."""
    n = len(bars)
    htf_cl = [None] * n
    htf_ema = [None] * n
    pdh = [None] * n
    pdl = [None] * n
    alpha = 2.0 / (HTF_LEN + 1)

    cur_h4 = None
    cur_close = None
    prev_close = None
    prev_ema = None
    ema_val = None
    cur_day = None
    cur_hi = cur_lo = None
    prev_hi = prev_lo = None

    for i, b in enumerate(bars):
        loc = b["t"].astimezone(NY) - timedelta(hours=17)
        k_day = (loc.year, loc.month, loc.day)
        k_h4 = (loc.year, loc.month, loc.day, loc.hour // 4)

        if k_h4 != cur_h4:
            if cur_h4 is not None:
                prev_close = cur_close
                ema_val = prev_close if ema_val is None else alpha * prev_close + (1 - alpha) * ema_val
                prev_ema = ema_val
            cur_h4 = k_h4
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
# Motor de senales (port fiel del Pine v9)
# ============================================================================
def run_engine(bars, v9=True):
    """v9=True: indicador v9. v9=False: indicador v8 (sin los 4 cambios del Modelo B)."""
    n = len(bars)
    O = [b["o"] for b in bars]
    H = [b["h"] for b in bars]
    L = [b["l"] for b in bars]
    C = [b["c"] for b in bars]
    VOL = [b["v"] for b in bars]
    if sum(VOL) <= 0:
        VOL = [1.0] * n  # sin volumen en el feed: perfil por tiempo (TPO)

    tr = [H[0] - L[0]] + [max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
                          for i in range(1, n)]
    atr = rma(tr, 14)

    pdm = [0.0]
    mdm = [0.0]
    for i in range(1, n):
        up = H[i] - H[i - 1]
        dn = L[i - 1] - L[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    trr = rma(tr, ADX_LEN)
    pr = rma(pdm, ADX_LEN)
    mr = rma(mdm, ADX_LEN)
    di_p = [None] * n
    di_m = [None] * n
    dx = [0.0] * n
    for i in range(n):
        if trr[i]:
            di_p[i] = 100.0 * pr[i] / trr[i]
            di_m[i] = 100.0 * mr[i] / trr[i]
            s = di_p[i] + di_m[i]
            dx[i] = abs(di_p[i] - di_m[i]) / (s if s != 0 else 1.0)
    adx_raw = rma(dx, ADX_LEN, start=ADX_LEN - 1)
    adx = [None if v is None else 100.0 * v for v in adx_raw]

    htf_cl, htf_ema, pdh, pdl = build_context(bars)

    swLen = PR["swLen"]
    intLen = PR["intLen"]
    mssWait = PR["mssWait"]
    setupWait = PR["setupWait"]
    chochWait = PR["chochWait"]
    microLen = PR["microLen"]
    cooldown = PR["cooldown"]
    obExtra = PR["obExtra"]
    minLegATR = PR["minLegATR"]
    dispATR = PR["dispATR"]
    fibA = PR["fibA"]
    fibB = PR["fibB"]
    microBodyATR = PR["microBodyATR"]
    maxRiskATR = PR["maxRiskATR"]

    def pivot_high(i, ln):
        c = i - ln
        if c - ln < 0:
            return None
        v = H[c]
        for j in range(c - ln, c):
            if H[j] >= v:
                return None
        for j in range(c + 1, i + 1):
            if H[j] > v:
                return None
        return v

    def pivot_low(i, ln):
        c = i - ln
        if c - ln < 0:
            return None
        v = L[c]
        for j in range(c - ln, c):
            if L[j] <= v:
                return None
        for j in range(c + 1, i + 1):
            if L[j] < v:
                return None
        return v

    def compute_poc(i):
        if not USE_VP or i < VP_LEN:
            return None
        lo_i = i - VP_LEN + 1
        vp_hi = max(H[lo_i:i + 1])
        vp_lo = min(L[lo_i:i + 1])
        if vp_hi <= vp_lo:
            return None
        bin_h = (vp_hi - vp_lo) / VP_ROWS
        vv = [0.0] * VP_ROWS
        for j in range(lo_i, i + 1):
            h3 = (H[j] + L[j] + C[j]) / 3.0
            idx = max(0, min(VP_ROWS - 1, int(math.floor((h3 - vp_lo) / bin_h))))
            vv[idx] += VOL[j]
        if sum(vv) <= 0:
            return None
        p_idx = vv.index(max(vv))
        return vp_lo + (p_idx + 0.5) * bin_h

    def compute_eq(i):
        a = max(0, i - PD_LEN + 1)
        return (max(H[a:i + 1]) + min(L[a:i + 1])) / 2.0

    sigs = []
    lastHigh = lastLow = None
    highUsed = lowUsed = False
    lastHighI = lastLowI = None
    lastHighIBar = lastLowIBar = None
    highIUsed = lowIUsed = False
    lastSig = -1000

    bState = 0
    bSweepBar = 0
    bLowPx = None
    bLowBar = 0
    bPdl = False
    bCont = False
    bMssBar = 0
    bHigh = None
    bTapBar = 0
    bTapLow = None
    bObV = bFvgV = False
    bPoiTop = bPoiBot = None

    sState = 0
    sSweepBar = 0
    sHighPx = None
    sHighBar = 0
    sPdh = False
    sCont = False
    sMssBar = 0
    sLow = None
    sTapBar = 0
    sTapHigh = None
    sObV = sFvgV = False
    sPoiTop = sPoiBot = None

    for i in range(n):
        a = atr[i]
        if a is None:
            continue
        o, h, l, c = O[i], H[i], L[i], C[i]
        c_prev = C[i - 1] if i > 0 else None

        # ---- pivotes ----
        ph = pivot_high(i, swLen)
        pl = pivot_low(i, swLen)
        phI = pivot_high(i, intLen)
        plI = pivot_low(i, intLen)
        if ph is not None:
            lastHigh = ph
            highUsed = False
        if pl is not None:
            lastLow = pl
            lowUsed = False
        if phI is not None:
            lastHighI = phI
            lastHighIBar = i - intLen
            highIUsed = False
        if plI is not None:
            lastLowI = plI
            lastLowIBar = i - intLen
            lowIUsed = False

        # maximos/minimos desde el ultimo pivote interno (Modelo B)
        hiSinceLowI = loSinceLowI = hiSinceHighI = loSinceHighI = None
        if lastLowIBar is not None:
            nL = min(max(i - lastLowIBar + 1, 1), 100)
            hiSinceLowI = max(H[i - nL + 1:i + 1])
            loSinceLowI = min(L[i - nL + 1:i + 1])
        if lastHighIBar is not None:
            nH = min(max(i - lastHighIBar + 1, 1), 100)
            hiSinceHighI = max(H[i - nH + 1:i + 1])
            loSinceHighI = min(L[i - nH + 1:i + 1])

        # ---- barridos ----
        pdl_i, pdh_i = pdl[i], pdh[i]
        bullSweepSwing = lastLow is not None and not lowUsed and l < lastLow and c > lastLow
        bearSweepSwing = lastHigh is not None and not highUsed and h > lastHigh and c < lastHigh
        bullSweepPdl = pdl_i is not None and l < pdl_i and c > pdl_i
        bearSweepPdh = pdh_i is not None and h > pdh_i and c < pdh_i
        bullSweepInt = INT_SWEEP_ON and lastLowI is not None and not lowIUsed and l < lastLowI and c > lastLowI
        bearSweepInt = INT_SWEEP_ON and lastHighI is not None and not highIUsed and h > lastHighI and c < lastHighI
        bullSweep = bullSweepSwing or bullSweepPdl or bullSweepInt
        bearSweep = bearSweepSwing or bearSweepPdh or bearSweepInt

        if bullSweepSwing or (lastLow is not None and c < lastLow):
            lowUsed = True
        if bearSweepSwing or (lastHigh is not None and c > lastHigh):
            highUsed = True
        if bullSweepInt or (lastLowI is not None and c < lastLowI):
            lowIUsed = True
        if bearSweepInt or (lastHighI is not None and c > lastHighI):
            highIUsed = True

        coolOK = (i - lastSig) > cooldown
        hc, he = htf_cl[i], htf_ema[i]
        htfBull = hc is not None and he is not None and hc > he
        htfBear = hc is not None and he is not None and hc < he
        trendUp = adx[i] is not None and adx[i] >= CONT_ADX and di_p[i] > di_m[i]
        trendDn = adx[i] is not None and adx[i] >= CONT_ADX and di_m[i] > di_p[i]

        hhMicroPrev = max(H[i - microLen:i]) if i >= microLen else None
        llMicroPrev = min(L[i - microLen:i]) if i >= microLen else None

        # ================= ALCISTA =================
        bullTrig = False
        bSl = None

        if bState == 1:
            if l < bLowPx:
                bLowPx = l
                bLowBar = i
            bHigh = max(bHigh, h)
            if bullSweep:
                bPdl = bPdl or bullSweepPdl
            if (lastHighI is not None and c > lastHighI and lastHighIBar > bLowBar
                    and i > bSweepBar and (c - o) >= dispATR * a):
                bState = 2
                bMssBar = i
            elif i - bLowBar > mssWait:
                bState = 0

        if bState == 2:
            R = bHigh - bLowPx
            faB = CONT_FIB_A if (bCont and v9) else fibA
            zTop = bHigh - faB * R
            zBot = bHigh - fibB * R
            legMin = (CONT_MIN_LEG_ATR if (bCont and v9) else minLegATR) * a
            if c < bLowPx or i - bMssBar > setupWait:
                bState = 0
            elif i > bMssBar and R >= legMin and l <= zTop:
                span = i - bLowBar
                obH = False
                obT = obB = None
                for k in range(span, span + obExtra + 1):
                    idx = i - k
                    if idx < 0:
                        break
                    if C[idx] < O[idx]:
                        obT, obB = H[idx], L[idx]
                        break
                if obT is not None and obT >= zBot and obB <= zTop:
                    obH = True
                fvH = False
                fvT = fvB = None
                if span >= 2:
                    fvgMin = FVG_MIN_ATR * a
                    for k in range(0, span - 1):
                        i0, i2 = i - k, i - k - 2
                        if i2 < 0:
                            break
                        if (L[i0] > H[i2] and (L[i0] - H[i2]) >= fvgMin
                                and H[i2] <= zTop and L[i0] >= zBot):
                            fvH = True
                            fvT, fvB = L[i0], H[i2]
                            break
                pTop = pBot = None
                if obH and fvH:
                    pTop, pBot = max(obT, fvT), min(obB, fvB)
                elif REQ_BOTH:
                    pTop = None
                elif fvH:
                    pTop, pBot = fvT, fvB
                elif obH:
                    pTop, pBot = obT, obB
                elif not REQ_POI:
                    pTop, pBot = zTop, zBot
                if pTop is not None and l <= pTop and c > pBot:
                    bState = 3
                    bTapBar = i
                    bTapLow = l
                    bObV = obH
                    bFvgV = fvH
                    bPoiTop, bPoiBot = pTop, pBot
            bHigh = max(bHigh, h)

        if bState == 3:
            bTapLow = min(bTapLow, l)
            if c < bLowPx or c < bPoiBot or i - bTapBar > chochWait:
                bState = 0
            else:
                slC = bTapLow - SL_BUF * a
                riskC = c - slC
                riskOK = (riskC >= MIN_RISK_ATR * a and riskC <= maxRiskATR * a
                          and (MAX_RISK_PX <= 0 or riskC <= MAX_RISK_PX))
                microUp = (c > o and hhMicroPrev is not None and c > hhMicroPrev
                           and (c - o) >= microBodyATR * a)
                if microUp and riskOK:
                    bullTrig = True
                    bSl = slC

        if bState == 0 and bullSweep:
            bState = 1
            bSweepBar = i
            bLowPx = l
            bLowBar = i
            bHigh = h
            bPdl = bullSweepPdl
            bCont = False

        if ((bState == 0 or (CONT_REARM and v9 and bState == 2 and i > bMssBar)) and CONT_ON and htfBull
                and lastHighI is not None and lastLowI is not None
                and lastHighIBar < lastLowIBar and (i - lastLowIBar) <= CONT_MAX_LEG
                and c > lastHighI and c_prev is not None and c_prev <= lastHighI
                and c > o and (c - o) >= dispATR * a and loSinceLowI >= lastLowI):
            bState = 2
            bMssBar = i
            bLowPx = lastLowI
            bLowBar = lastLowIBar
            bHigh = hiSinceLowI
            bPdl = False
            bCont = True

        buySig = False
        bScore = 0
        if bullTrig:
            poc = compute_poc(i)
            eq = compute_eq(i)
            bScore += 1 if bPdl else 0
            bScore += 1 if bObV else 0
            bScore += 1 if bFvgV else 0
            bScore += 1 if (USE_HTF and htfBull) else 0
            bScore += (1 if trendUp else 0) if (bCont and v9) else (1 if c < eq else 0)
            vpB = 1 if (USE_VP and poc is not None and c <= poc) else 0
            bScore += vpB
            need = CONT_MIN_SCORE if bCont else MIN_SCORE
            bNeed = need
            buySig = bScore >= need and coolOK and (not REQ_HTF or htfBull)

        # ================= BAJISTA =================
        bearTrig = False
        sSl = None

        if sState == 1:
            if h > sHighPx:
                sHighPx = h
                sHighBar = i
            sLow = min(sLow, l)
            if bearSweep:
                sPdh = sPdh or bearSweepPdh
            if (lastLowI is not None and c < lastLowI and lastLowIBar > sHighBar
                    and i > sSweepBar and (o - c) >= dispATR * a):
                sState = 2
                sMssBar = i
            elif i - sHighBar > mssWait:
                sState = 0

        if sState == 2:
            Rs = sHighPx - sLow
            faS = CONT_FIB_A if (sCont and v9) else fibA
            zBotS = sLow + faS * Rs
            zTopS = sLow + fibB * Rs
            legMinS = (CONT_MIN_LEG_ATR if (sCont and v9) else minLegATR) * a
            if c > sHighPx or i - sMssBar > setupWait:
                sState = 0
            elif i > sMssBar and Rs >= legMinS and h >= zBotS:
                spanS = i - sHighBar
                obHs = False
                obTs = obBs = None
                for k in range(spanS, spanS + obExtra + 1):
                    idx = i - k
                    if idx < 0:
                        break
                    if C[idx] > O[idx]:
                        obTs, obBs = H[idx], L[idx]
                        break
                if obTs is not None and obTs >= zBotS and obBs <= zTopS:
                    obHs = True
                fvHs = False
                fvTs = fvBs = None
                if spanS >= 2:
                    fvgMin = FVG_MIN_ATR * a
                    for k in range(0, spanS - 1):
                        i0, i2 = i - k, i - k - 2
                        if i2 < 0:
                            break
                        if (H[i0] < L[i2] and (L[i2] - H[i0]) >= fvgMin
                                and H[i0] <= zTopS and L[i2] >= zBotS):
                            fvHs = True
                            fvTs, fvBs = L[i2], H[i0]
                            break
                pTopS = pBotS = None
                if obHs and fvHs:
                    pTopS, pBotS = max(obTs, fvTs), min(obBs, fvBs)
                elif REQ_BOTH:
                    pTopS = None
                elif fvHs:
                    pTopS, pBotS = fvTs, fvBs
                elif obHs:
                    pTopS, pBotS = obTs, obBs
                elif not REQ_POI:
                    pTopS, pBotS = zTopS, zBotS
                if pBotS is not None and h >= pBotS and c < pTopS:
                    sState = 3
                    sTapBar = i
                    sTapHigh = h
                    sObV = obHs
                    sFvgV = fvHs
                    sPoiTop, sPoiBot = pTopS, pBotS
            sLow = min(sLow, l)

        if sState == 3:
            sTapHigh = max(sTapHigh, h)
            if c > sHighPx or c > sPoiTop or i - sTapBar > chochWait:
                sState = 0
            else:
                slS = sTapHigh + SL_BUF * a
                riskS = slS - c
                riskOKs = (riskS >= MIN_RISK_ATR * a and riskS <= maxRiskATR * a
                           and (MAX_RISK_PX <= 0 or riskS <= MAX_RISK_PX))
                microDn = (c < o and llMicroPrev is not None and c < llMicroPrev
                           and (o - c) >= microBodyATR * a)
                if microDn and riskOKs:
                    bearTrig = True
                    sSl = slS

        if sState == 0 and bearSweep:
            sState = 1
            sSweepBar = i
            sHighPx = h
            sHighBar = i
            sLow = l
            sPdh = bearSweepPdh
            sCont = False

        if ((sState == 0 or (CONT_REARM and v9 and sState == 2 and i > sMssBar)) and CONT_ON and htfBear
                and lastHighI is not None and lastLowI is not None
                and lastLowIBar < lastHighIBar and (i - lastHighIBar) <= CONT_MAX_LEG
                and c < lastLowI and c_prev is not None and c_prev >= lastLowI
                and c < o and (o - c) >= dispATR * a and hiSinceHighI <= lastHighI):
            sState = 2
            sMssBar = i
            sHighPx = lastHighI
            sHighBar = lastHighIBar
            sLow = loSinceHighI
            sPdh = False
            sCont = True

        sellSig = False
        sScore = 0
        if bearTrig:
            poc = compute_poc(i)
            eq = compute_eq(i)
            sScore += 1 if sPdh else 0
            sScore += 1 if sObV else 0
            sScore += 1 if sFvgV else 0
            sScore += 1 if (USE_HTF and htfBear) else 0
            sScore += (1 if trendDn else 0) if (sCont and v9) else (1 if c > eq else 0)
            vpS = 1 if (USE_VP and poc is not None and c >= poc) else 0
            sScore += vpS
            need = CONT_MIN_SCORE if sCont else MIN_SCORE
            sNeed = need
            sellSig = sScore >= need and coolOK and (not REQ_HTF or htfBear)

        # ================= Ejecucion =================
        if buySig:
            lastSig = i
            sigs.append(dict(side="COMPRA", i=i, label_i=bLowBar, close=c, sl=bSl,
                             score=bScore, model="B" if bCont else "A",
                             ver="v9" if v9 else "v8", vp_decisive=(bScore - vpB) < bNeed))
            bState = 0
        if sellSig:
            lastSig = i
            sigs.append(dict(side="VENTA", i=i, label_i=sHighBar, close=c, sl=sSl,
                             score=sScore, model="B" if sCont else "A",
                             ver="v9" if v9 else "v8", vp_decisive=(sScore - vpS) < sNeed))
            sState = 0

    return sigs


# ============================================================================
# Telegram / estado / mensajes
# ============================================================================
VELAS_N = 150  # cuantas velas imprime el modo 'velas'


def fmt_t(t):
    return t.astimezone(EC).strftime("%d/%m %H:%M")


def _hm(mins):
    return "%02d:%02d" % ((mins // 60) % 24, mins % 60)


def killzone_text(t_open):
    """Dice si la vela (por su hora de APERTURA, igual que el Pine) esta dentro de una killzone.
    Devuelve (texto para el mensaje, nombre de la killzone o None)."""
    ny = t_open.astimezone(NY)
    mins = ny.hour * 60 + ny.minute
    day0 = datetime(ny.year, ny.month, ny.day, tzinfo=NY)

    def span(a, b):
        ea = (day0 + timedelta(minutes=a)).astimezone(EC).strftime("%H:%M")
        eb = (day0 + timedelta(minutes=b)).astimezone(EC).strftime("%H:%M")
        return "%s-%s Ecuador / %s-%s NY" % (ea, eb, _hm(a), _hm(b))

    for name, a, b in KILLZONES:
        inside = (a <= mins < b) if a <= b else (mins >= a or mins < b)
        if inside:
            return "\u2705 Killzone: SI, dentro de %s (%s)" % (name, span(a, b)), name
    detalle = " y ".join("%s (%s)" % (name, span(a, b)) for name, a, b in KILLZONES)
    return "\u26AA Killzone: NO, fuera de %s" % detalle, None


def parse_event_time(text):
    dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)             # Forex Factory publica en hora del Este si no trae zona
    return dt.astimezone(timezone.utc)


def fetch_news():
    """Baja el calendario de la semana UNA vez por ejecucion (solo cuando hay un aviso que mandar).
    Devuelve la lista de eventos (hora UTC, titulo, pais) o None si no se pudo consultar."""
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
    """Lineas de advertencia para el mensaje: SOLO si hay una noticia roja (impacto alto) de USD cerca.
    Si no hay ninguna, no se agrega nada. Si el calendario no responde, se dice (el aviso sale igual)."""
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
    """Modo 'noticias': muestra el calendario que ve el bot y lo que diria un aviso enviado ahora."""
    now = datetime.now(timezone.utc)
    print("Ahora: %s hora Ecuador / %s hora NY" % (now.astimezone(EC).strftime("%d/%m %H:%M"),
                                                   now.astimezone(NY).strftime("%d/%m %H:%M")))
    print("Filtro: paises %s | impacto %s (rojo = high) | avisa si falta menos de %d min o salio hace menos de %d min"
          % ("/".join(sorted(NEWS_COUNTRIES)), "/".join(sorted(NEWS_IMPACTS)), NEWS_BEFORE_MIN, NEWS_AFTER_MIN))
    events = fetch_news()
    if events is None:
        print("No se pudo consultar el calendario (mira el mensaje de arriba).")
        return
    if not events:
        print("El calendario no trae eventos con ese filtro esta semana.")
        return
    print("Eventos de la semana (compara con forexfactory.com/calendar):")
    print("  dia   hora Ecuador | hora NY | evento")
    for dt, title, country in sorted(events):
        e, ny = dt.astimezone(EC), dt.astimezone(NY)
        print("  %s %s   %s | %s | %s %s" % (_DIAS[e.weekday()], e.strftime("%d/%m"), e.strftime("%H:%M"),
                                              ny.strftime("%H:%M"), country, title))
    lines = news_lines(now)
    print()
    print("Lo que diria un aviso enviado AHORA:")
    print("  " + ("(nada: no hay noticias rojas cerca)" if not lines else "\n  ".join(lines)))


def merge_signals(*lists):
    """Junta las senales de v8 y v9 en UN aviso por etiqueta.
    1) misma vela de confirmacion y mismo lado -> un solo grupo [v8 + v9];
    2) si otro indicador confirma MAS TARDE la misma etiqueta (mismo pico/suelo),
       no se avisa de nuevo: se anota en 'dups' (solo para el modo history)."""
    groups = {}
    for lst in lists:
        for s in lst:
            groups.setdefault((s["side"], s["i"]), []).append(s)
    ordered = [dict(side=side, i=i, items=sorted(v, key=lambda x: x["ver"]), dups=[])
               for (side, i), v in groups.items()]
    ordered.sort(key=lambda g: (g["i"], g["side"]))

    seen = {}   # (lado, vela de la etiqueta) -> {"vers": versiones ya avisadas, "group": grupo}
    out = []
    for g in ordered:
        keep = []
        for x in g["items"]:
            k = (g["side"], x["label_i"])
            prev = seen.get(k)
            if prev and x["ver"] not in prev["vers"]:
                prev["group"]["dups"].append(x)
                continue
            keep.append(x)
        if not keep:
            continue
        g["items"] = keep
        for x in keep:
            k = (g["side"], x["label_i"])
            seen.setdefault(k, {"vers": set(), "group": g})["vers"].add(x["ver"])
        out.append(g)
    return out


def build_message(group, bars, now=None):
    items = group["items"]
    b = bars[group["i"]]
    close_time = b["t"] + BAR
    side = group["side"]
    icon = "\U0001F7E2" if side == "COMPRA" else "\U0001F534"
    tag = " + ".join(x["ver"] for x in items)
    close = items[0]["close"]

    lines = ["%s %s XAU/USD %s  [%s]" % (icon, side, TF_INFO[TF]["label"], tag)]
    for x in items:
        modelo = "B (continuacion)" if x["model"] == "B" else "A (barrido)"
        maxs = 5 if x["model"] == "B" else 6
        lines.append("%s: Modelo %s | Puntuacion %d/%d" % (x["ver"], modelo, x["score"], maxs))
    lines.append("Cierre de vela: %.2f" % close)

    def sl_tp(x):
        risk = abs(x["close"] - x["sl"])
        tp = x["close"] + RR * risk if side == "COMPRA" else x["close"] - RR * risk
        return risk, tp

    if len(items) == 1 or all(abs(x["sl"] - items[0]["sl"]) < 0.005 for x in items):
        risk, tp = sl_tp(items[0])
        lines.append("SL: %.2f (riesgo $%.2f)" % (items[0]["sl"], risk))
        lines.append("TP 1:%g: %.2f" % (RR, tp))
    else:
        for x in items:
            risk, tp = sl_tp(x)
            lines.append("%s -> SL %.2f (riesgo $%.2f) | TP 1:%g %.2f" % (x["ver"], x["sl"], risk, RR, tp))
    lines.append("Vela %s cerrada: %s (hora Ecuador)" % (tf_name(), fmt_t(close_time)))
    
    # Las lineas extra son informativas: jamas pueden frenar un aviso
    try:
        lines.append(killzone_text(b["t"])[0])
    except Exception as e:
        print("AVISO: no se pudo calcular la killzone (%s); el aviso sale igual." % type(e).__name__)
        
    try:
        lines.extend(news_lines(now or datetime.now(timezone.utc)))
    except Exception as e:
        print("AVISO: no se pudo revisar las noticias (%s); el aviso sale igual." % type(e).__name__)

    lines.append("")
    if DATA_SOURCE == "capital":
        lines.append("Precios de Capital.com (%s), el mismo feed de tu grafico con fuente Capital.com. "
                     "Confirma en tu grafico antes de operar." % CAPITAL_PRICE)
    else:
        lines.append("Precios del feed Twelve Data: pueden diferir unos dolares de TradingView. "
                     "Confirma en tu grafico antes de operar.")
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
    """Clave para no avisar dos veces la misma senal. En M15 queda igual que antes."""
    stamp = t.strftime("%Y-%m-%dT%H:%M")
    if TF == 15:
        return "%s|%s" % (side, stamp)
    return "%s|%s|%s" % (side, tf_name(), stamp)


# ============================================================================
# Modos de diagnostico
# ============================================================================
def print_history(groups, bars):
    print("Senales en el historial (%d). Comparalas con las etiquetas de TradingView:" % len(groups))
    print("(* = la senal depende del punto del perfil de volumen)")
    for g in groups:
        x = g["items"][0]
        tags = " + ".join(y["ver"] for y in g["items"])
        det = " / ".join("%s %s p%d%s" % (y["ver"], y["model"], y["score"], "*" if y["vp_decisive"] else "")
                         for y in g["items"])
        kz = killzone_text(bars[g["i"]]["t"])[1] or "fuera"
        print("%-6s [%-7s] %s | etiqueta en %s | confirmada %s | cierre %.2f SL %.2f | KZ: %s"
              % (g["side"], tags, det, fmt_t(bars[x["label_i"]]["t"]),
                 fmt_t(bars[g["i"]]["t"] + BAR), x["close"], x["sl"], kz))
        for d in g["dups"]:
            print("       + %s confirma la misma etiqueta a las %s (no se avisa aparte)"
                  % (d["ver"], fmt_t(bars[d["i"]]["t"] + BAR)))


def run_capital_check():
    """Prueba la conexion con Capital.com: abre sesion, baja velas e imprime las ultimas."""
    if DATA_SOURCE != "capital":
        sys.exit("Este modo prueba Capital.com: pon DATA_SOURCE=capital")
    raw = fetch_capital(TF, 1)
    now = datetime.now(timezone.utc)
    print("Sesion abierta en Capital.com (%s) | epic %s | precio %s | temporalidad %s | velas recibidas: %d"
          % (CAPITAL_ENV_USED, CAPITAL_EPIC, CAPITAL_PRICE, tf_name(), len(raw)))
    print("Primera vela: %s | ultima vela recibida: %s (hora Ecuador)" % (fmt_t(raw[0]["t"]), fmt_t(raw[-1]["t"])))
    print("Ahora: %s (hora Ecuador)" % fmt_t(now))
    bars = clean_bars(raw, now)
    if not bars:
        print("No hay velas cerradas con mercado abierto (fin de semana?).")
        return
    gaps = [(bars[k]["t"] - bars[k - 1]["t"]).total_seconds() / 60 for k in range(1, len(bars))]
    print("Cierres de mercado dentro del historial (huecos de mas de 3 velas): %d"
          % sum(1 for g in gaps if g > 3 * TF))
    print()
    print("Ultimas 12 velas %s cerradas, hora de APERTURA (hora Ecuador, igual que TradingView):" % tf_name())
    print("hora         |   apertura    maximo     minimo     cierre")
    for b in bars[-12:]:
        print("%s | %10.2f %10.2f %10.2f %10.2f" % (fmt_t(b["t"]), b["o"], b["h"], b["l"], b["c"]))
    print()
    print("Compara 2 o 3 de estas velas con las de tu grafico (pasa el mouse sobre la vela en TradingView).")


# ============================================================================
# Revision de una temporalidad (modo normal)
# ============================================================================
def check_timeframe(tf, now, state, raw=None):
    """Revisa una temporalidad y manda los avisos nuevos. Devuelve True si hubo avisos (cambia el estado)."""
    set_tf(tf)
    name = tf_name()
    if raw is None:
        raw = fetch_bars(tf, False)
    else:
        now = raw[-1]["t"] + timedelta(days=1)     # (pruebas con CSV)
    bars = clean_bars(raw, now)
    if len(bars) < 300:
        sys.exit("Muy pocas velas utiles (%d) en %s. Revisa el feed." % (len(bars), name))
    last_close_time = bars[-1]["t"] + BAR
    age_last = (now - last_close_time).total_seconds() / 60.0
    print("[%s] Fuente: %s | velas: %d | ultima cerrada: %s UTC | ahora: %s UTC | atraso del feed: %.0f min | modo %s"
          % (name, source_label(), len(bars), last_close_time.strftime("%Y-%m-%d %H:%M"),
             now.strftime("%Y-%m-%d %H:%M"), age_last, FREQ))

    groups = merge_signals(run_engine(bars, v9=False), run_engine(bars, v9=True))
    n = len(bars)
    fresh = [g for g in groups if g["i"] >= n - LOOKBACK_BARS]
    changed = False
    if not fresh:
        print("[%s] Sin senales nuevas en las ultimas %d velas." % (name, LOOKBACK_BARS))
    for s in fresh:
        key = state_key(s["side"], bars[s["i"]]["t"])
        if key in state["sent"]:
            print("[%s] Ya avisada: %s" % (name, key))
            continue
        age = (now - (bars[s["i"]]["t"] + BAR)).total_seconds() / 60.0
        if age > MAX_AGE_MIN:
            print("[%s] Senal %s demasiado vieja (%.0f min), no se avisa." % (name, key, age))
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
    ap.add_argument("--mode", default="normal", choices=["normal", "test", "history", "velas", "capital", "noticias"])
    ap.add_argument("--tf", type=int, default=15, choices=sorted(TF_INFO),
                    help="temporalidad en minutos para capital / history / velas (15, 30 o 60)")
    ap.add_argument("--csv", help="(pruebas) leer velas de un CSV en vez de la API")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)

    if args.mode == "test":
        ok = send_telegram("\u2705 Bot de senales conectado. Si ves este mensaje, Telegram funciona.")
        print("Mensaje de prueba enviado" if ok else "No se pudo enviar el mensaje de prueba")
        sys.exit(0 if ok else 1)

    if args.mode == "capital":
        set_tf(args.tf)
        run_capital_check()
        return

    if args.mode == "noticias":
        run_news_check()
        return

    if args.mode == "normal":
        if not args.csv:
            now = wait_for_close(now)                        # si el cierre esta cerca, espera al cierre
            # se mira 1 minuto atras para no saltarse la ultima vela antes del cierre semanal
            if not market_open(now - timedelta(minutes=1)):
                print("Mercado cerrado, nada que revisar.")
                return
        state = load_state()
        if args.csv:                                         # (pruebas) una sola temporalidad desde CSV
            if check_timeframe(args.tf, now, state, raw=load_csv(args.csv)):
                save_state(state)
            return
        ok_count = 0
        errors = []
        for tf in TIMEFRAMES:
            try:
                if check_timeframe(tf, now, state):
                    save_state(state)                        # se guarda ya, por si falla la siguiente
                ok_count += 1
            except SystemExit as e:
                msg = str(e.code) if e.code is not None else "error"
                print("[%s] ERROR: %s" % (TF_INFO[tf]["name"], msg))
                errors.append("%s: %s" % (TF_INFO[tf]["name"], msg))
        if ok_count == 0:
            sys.exit("Ninguna temporalidad pudo revisarse. " + " | ".join(errors))
        if errors:
            print("AVISO: fallaron estas temporalidades (las demas se revisaron): " + " | ".join(errors))
        return

    # ---- modos de diagnostico (history / velas): una sola temporalidad ----
    set_tf(args.tf)
    raw = load_csv(args.csv) if args.csv else fetch_bars(args.tf, True)
    if args.csv and raw:
        now = raw[-1]["t"] + timedelta(days=1)
    bars = clean_bars(raw, now)
    if len(bars) < 300:
        sys.exit("Muy pocas velas utiles (%d). Revisa el feed." % len(bars))
    last_close_time = bars[-1]["t"] + BAR
    age_last = (now - last_close_time).total_seconds() / 60.0
    print("[%s] Fuente: %s | velas: %d | ultima cerrada: %s UTC | ahora: %s UTC | atraso del feed: %.0f min | modo %s"
          % (tf_name(), source_label(), len(bars), last_close_time.strftime("%Y-%m-%d %H:%M"),
             now.strftime("%Y-%m-%d %H:%M"), age_last, FREQ))

    groups = merge_signals(run_engine(bars, v9=False), run_engine(bars, v9=True))

    if args.mode == "history":
        print("(ignora las senales de los primeros dias: el indicador necesita velas para calentarse)")
        print_history(groups, bars)
        return

    # velas: ultimas velas con hora de APERTURA (hora Ecuador, como TradingView) y su O/H/L/C
    marks = {}
    for g in groups:
        for it in g["items"] + g["dups"]:
            tag = "%s %s" % (g["side"], it["ver"])
            marks.setdefault(it["label_i"], []).append("ETIQUETA " + tag)
        marks.setdefault(g["i"], []).append("aviso %s" % g["side"])
    last = bars[-VELAS_N:]
    first_i = len(bars) - len(last)
    print("Ultimas %d velas %s, hora de apertura = hora Ecuador (UTC-5):" % (len(last), tf_name()))
    print("hora         |   apertura    maximo     minimo     cierre | marcas")
    for k, b in enumerate(last):
        m = ", ".join(marks.get(first_i + k, []))
        print("%s | %10.2f %10.2f %10.2f %10.2f | %s"
              % (fmt_t(b["t"]), b["o"], b["h"], b["l"], b["c"], m))


if __name__ == "__main__":
    main()
