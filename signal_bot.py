#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oro SMC v8 + v9 -> avisos por Telegram.
Corre en GitHub Actions cada 5 minutos. Cada ejecucion:
  1) baja ~3000 velas de XAU/USD 15m de Twelve Data,
  2) repasa TODA la logica de los indicadores v8 y v9 vela por vela,
  3) si una de las ultimas 2 velas cerradas disparo COMPRA/VENTA (en v8, en v9
     o en ambos) y no se aviso antes, manda UN mensaje por Telegram.

Modos:  python signal_bot.py --mode normal    (el de siempre)
        python signal_bot.py --mode test      (manda un mensaje de prueba)
        python signal_bot.py --mode history   (imprime las senales de todo el
                                               historial, para comparar con TradingView)
Variables (GitHub Secrets): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_KEY
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
BAR = timedelta(minutes=15)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

SYMBOL = os.getenv("SYMBOL", "XAU/USD")
OUTPUTSIZE = int(os.getenv("OUTPUTSIZE", "3000"))
FREQ = os.getenv("FREQ", "Equilibrado")  # Estricto | Equilibrado | Alto
LOOKBACK_BARS = 2   # avisa si la senal ocurrio en una de las ultimas N velas cerradas
MAX_AGE_MIN = 45    # no avisa senales mas viejas que esto (datos atrasados / reinicios)

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


def parse_rows(rows):
    bars = []
    for v in rows:
        t = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        bars.append(dict(t=t, o=float(v["open"]), h=float(v["high"]), l=float(v["low"]),
                         c=float(v["close"]), v=float(v.get("volume") or 0)))
    bars.sort(key=lambda b: b["t"])
    out = []
    for b in bars:
        if not out or b["t"] > out[-1]["t"]:
            out.append(b)
    return out


def fetch_bars():
    key = os.getenv("TWELVEDATA_KEY")
    if not key:
        sys.exit("Falta el secret TWELVEDATA_KEY")
    params = dict(symbol=SYMBOL, interval="15min", outputsize=OUTPUTSIZE, order="ASC",
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


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return parse_rows(list(csv.DictReader(f)))


def clean_bars(bars, now):
    """Quita velas con mercado cerrado y la vela que aun no cierra."""
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


def build_message(group, bars):
    items = group["items"]
    b = bars[group["i"]]
    close_time = b["t"] + BAR
    side = group["side"]
    icon = "\U0001F7E2" if side == "COMPRA" else "\U0001F534"
    tag = " + ".join(x["ver"] for x in items)
    close = items[0]["close"]

    lines = ["%s %s %s 15m  [%s]" % (icon, side, SYMBOL, tag)]
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
    lines.append("Vela cerrada: %s (hora Ecuador)" % fmt_t(close_time))
    lines.append("")
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
    state["sent"] = state["sent"][-60:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
        f.write("\n")


# ============================================================================
# Main
# ============================================================================
# ============================================================================
# Modo feeds: compara Twelve Data y Dukascopy contra velas medidas en TradingView
# ============================================================================
TV_REF = [
    # (hora UTC de apertura, apertura, maximo, minimo, cierre) medidas en TradingView (OANDA)
    ("2026-10-22 15:00", 4059.635, 4063.105, 4053.845, 4055.010),   # 22/10 10:00 Ecuador
    ("2026-10-22 15:15", 4055.100, 4061.150, 4051.590, 4051.840),   # 22/10 10:15 Ecuador
    ("2026-10-23 02:00", 3992.155, 4014.140, 3986.730, 4013.170),   # 22/10 21:00 Ecuador
]
DUKA_LAST_HEADERS = {}
DUKA_DAYS = 32   # dias que baja el modo histduka
DUKA_URL = "https://datafeed.dukascopy.com/datafeed/XAUUSD/%04d/%02d/%02d/%s_candles_min_1.bi5"


DUKA_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duka_cache")


def duka_day(d, side):
    """Baja las velas de 1 minuto de un dia (UTC) de Dukascopy. Devuelve (dict minuto->vela, nota).
    Los dias ya cerrados se guardan en disco: si una corrida se corta, la siguiente continua."""
    import lzma
    import struct
    today = datetime.now(timezone.utc).date()
    closed = d < today
    cpath = os.path.join(DUKA_CACHE, "%s_%s.bi5" % (d.isoformat(), side))
    content = None
    if closed and os.path.exists(cpath):
        with open(cpath, "rb") as f:
            content = f.read()
        if not content:
            return {}, "sin datos (guardado)"
    if content is None:
        url = DUKA_URL % (d.year, d.month - 1, d.day, side)   # el mes cuenta desde 0
        r = None
        err = "?"
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=(30, 60), headers={"User-Agent": "Mozilla/5.0"})
            except requests.RequestException as e:
                r = None
                err = type(e).__name__
                time.sleep(3)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                err = "HTTP %d" % r.status_code
                time.sleep(6 * (attempt + 1))        # el servidor esta saturado: esperar y reintentar
                continue
            break
        if r is None:
            return {}, "sin conexion (%s)" % err
        global DUKA_LAST_HEADERS
        try:
            DUKA_LAST_HEADERS = dict(r.headers)
        except (TypeError, ValueError, AttributeError):
            DUKA_LAST_HEADERS = {}
        if r.status_code != 200 or not r.content:
            note = "HTTP %d" % r.status_code if r.status_code != 200 else "archivo vacio"
            if closed and (today - d).days >= 3 and r.status_code in (200, 404):
                os.makedirs(DUKA_CACHE, exist_ok=True)       # dia sin datos de verdad (fin de semana)
                open(cpath, "wb").close()
            return {}, note
        content = r.content
        if closed:
            os.makedirs(DUKA_CACHE, exist_ok=True)
            with open(cpath, "wb") as f:
                f.write(content)
    try:
        buf = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO).decompress(content)
    except lzma.LZMAError as e:
        return {}, "no se pudo descomprimir (%s)" % e
    n = len(buf) // 24
    if n == 0:
        return {}, "sin velas"
    recs = [struct.unpack(">5If", buf[i * 24:(i + 1) * 24]) for i in range(n)]

    def ok_a(x):                      # orden (apertura, cierre, minimo, maximo)
        o, c, l, h = x[1:5]
        return l <= min(o, c) and h >= max(o, c)

    def ok_b(x):                      # orden (apertura, maximo, minimo, cierre)
        o, h, l, c = x[1:5]
        return l <= min(o, c) and h >= max(o, c)

    na = sum(1 for x in recs if ok_a(x))
    nb = sum(1 for x in recs if ok_b(x))
    order = "A" if na >= nb else "B"
    vals = sorted(x[1] for x in recs)
    med = vals[len(vals) // 2]
    div = next((dv for dv in (1000, 100000, 100, 10, 1) if 500 < med / dv < 20000), 1000)
    day0 = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    out = {}
    for x in recs:
        if order == "A":
            o, c, l, h = [v / div for v in x[1:5]]
        else:
            o, h, l, c = [v / div for v in x[1:5]]
        out[day0 + timedelta(seconds=x[0])] = dict(o=o, h=h, l=l, c=c)
    return out, "%d velas de 1 min | orden %s (%d de %d coherentes) | divisor %d" % (n, order, max(na, nb), n, div)


def duka_candle(bid, ask, t0, half=0.0):
    """Arma la vela de 15 min (precio medio bid/ask, como muestra TradingView) desde las de 1 min.
    Si de un minuto solo hay un lado, se ajusta con la mitad del spread ('half') para llegar al medio."""
    rows = []
    for k in range(15):
        m = t0 + timedelta(minutes=k)
        b, a = bid.get(m), ask.get(m)
        if b and a:
            rows.append({f: (b[f] + a[f]) / 2 for f in "ohlc"})
        elif b:
            rows.append({f: b[f] + half for f in "ohlc"})
        elif a:
            rows.append({f: a[f] - half for f in "ohlc"})
    if not rows:
        return None
    return dict(o=rows[0]["o"], h=max(x["h"] for x in rows), l=min(x["l"] for x in rows),
                c=rows[-1]["c"])


def run_feeds(args):
    raw = load_csv(args.csv) if args.csv else fetch_bars()
    td = {b["t"]: b for b in raw}
    refs = []
    for hs, o, h, l, c in TV_REF:
        t = datetime.strptime(hs, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        refs.append((t, dict(o=o, h=h, l=l, c=c)))
    duka = {"BID": {}, "ASK": {}}
    for d in sorted({t.date() for t, _ in refs}):
        for side in ("BID", "ASK"):
            got, note = duka_day(d, side)
            duka[side].update(got)
            print("Dukascopy %s %s: %s" % (side, d.isoformat(), note))
    print()

    def show(name, v):
        print("  %-18s %10.2f %10.2f %10.2f %10.2f %8.2f" % (name, v["o"], v["h"], v["l"], v["c"], v["h"] - v["l"]))

    errs = {"Twelve Data": [], "Dukascopy": []}
    for t, tv in refs:
        print("Vela %s (hora Ecuador) = %s UTC" % (fmt_t(t), t.strftime("%Y-%m-%d %H:%M")))
        print("  %-18s %10s %10s %10s %10s %8s" % ("fuente", "apertura", "maximo", "minimo", "cierre", "rango"))
        show("TradingView", tv)
        for key, name, v in (("Twelve Data", "Twelve Data", td.get(t)),
                             ("Dukascopy", "Dukascopy (medio)", duka_candle(duka["BID"], duka["ASK"], t))):
            if v is None:
                print("  %-18s sin dato para esta vela" % name)
                continue
            show(name, v)
            diff = [v[f] - tv[f] for f in "ohlc"]
            print("  %-18s %+10.2f %+10.2f %+10.2f %+10.2f" % ("   dif. vs TV", *diff))
            errs[key].extend(abs(x) for x in diff)
        print()
    print("RESUMEN (diferencia media absoluta contra TradingView, en dolares):")
    means = {}
    for key, lst in errs.items():
        if lst:
            means[key] = sum(lst) / len(lst)
            print("  %-12s %.2f  (%d valores)" % (key, means[key], len(lst)))
        else:
            print("  %-12s sin datos" % key)
    if len(means) == 2:
        best = min(means, key=means.get)
        gap = abs(means["Twelve Data"] - means["Dukascopy"])
        print("Mas cercano a TradingView: %s%s" % (best, " (casi empate)" if gap < 0.15 else ""))


def run_dukalive(args):
    """Mide que tan al dia esta el archivo de HOY de Dukascopy y compara las ultimas velas con Twelve Data."""
    now = datetime.now(timezone.utc)
    print("Ahora: %s UTC (%s hora Ecuador)" % (now.strftime("%Y-%m-%d %H:%M:%S"), fmt_t(now)))
    day = now.date()
    sides = {}
    for side in ("BID", "ASK"):
        got, note = duka_day(day, side)
        sides[side] = got
        print("Dukascopy %s %s: %s" % (side, day.isoformat(), note))
        h = {k.lower(): v for k, v in DUKA_LAST_HEADERS.items()}
        print("   cabeceras: last-modified=%s | date=%s | age=%s | cache-control=%s"
              % (h.get("last-modified", "-"), h.get("date", "-"), h.get("age", "-"), h.get("cache-control", "-")))
        if got:
            last = max(got)
            lag = (now - (last + timedelta(minutes=1))).total_seconds() / 60.0
            print("   ultima vela de 1 min: %s UTC | retraso respecto a ahora: %.1f min"
                  % (last.strftime("%Y-%m-%d %H:%M"), lag))
    print()
    raw = load_csv(args.csv) if args.csv else fetch_bars()
    td = {b["t"]: b for b in raw}
    start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    print("Ultimas velas de 15 min ya cerradas (Dukascopy medio contra Twelve Data):")
    print("  %-12s %-12s %10s %10s %10s %10s" % ("hora Ecuador", "fuente", "apertura", "maximo", "minimo", "cierre"))
    for k in range(6, 0, -1):
        t = start - timedelta(minutes=15 * k)
        dv = duka_candle(sides["BID"], sides["ASK"], t)
        tv = td.get(t)
        if dv:
            print("  %-12s %-12s %10.2f %10.2f %10.2f %10.2f" % (fmt_t(t), "Dukascopy", dv["o"], dv["h"], dv["l"], dv["c"]))
        else:
            print("  %-12s %-12s sin datos de Dukascopy" % (fmt_t(t), "Dukascopy"))
        if tv:
            print("  %-12s %-12s %10.2f %10.2f %10.2f %10.2f" % ("", "Twelve Data", tv["o"], tv["h"], tv["l"], tv["c"]))
        else:
            print("  %-12s %-12s sin datos de Twelve Data" % ("", "Twelve Data"))


def print_history(groups, bars):
    print("Senales en el historial (%d). Comparalas con las etiquetas de TradingView:" % len(groups))
    print("(* = la senal depende del punto del perfil de volumen, que aqui se calcula sin volumen real)")
    for g in groups:
        x = g["items"][0]
        tags = " + ".join(y["ver"] for y in g["items"])
        det = " / ".join("%s %s p%d%s" % (y["ver"], y["model"], y["score"], "*" if y["vp_decisive"] else "")
                         for y in g["items"])
        print("%-6s [%-7s] %s | etiqueta en %s | confirmada %s | cierre %.2f SL %.2f"
              % (g["side"], tags, det, fmt_t(bars[x["label_i"]]["t"]),
                 fmt_t(bars[g["i"]]["t"] + BAR), x["close"], x["sl"]))
        for d in g["dups"]:
            print("       + %s confirma la misma etiqueta a las %s (no se avisa aparte)"
                  % (d["ver"], fmt_t(bars[d["i"]]["t"] + BAR)))


def duka_bars(days_back, end_day):
    """Velas de 15 min (precio medio) de Dukascopy de los ultimos 'days_back' dias UTC hasta end_day.
    Devuelve (velas, dias_sin_nada, dias_con_un_solo_lado)."""
    from concurrent.futures import ThreadPoolExecutor
    days = [end_day - timedelta(days=k) for k in range(days_back)]          # del mas nuevo al mas viejo
    jobs = [(d, "BID") for d in days] + [(d, "ASK") for d in days]      # primero BID de todos los dias
    t_limit = time.time() + 540

    def work(job):
        d, side = job
        if time.time() > t_limit:
            return job, {}, "omitido por tiempo"
        got, note = duka_day(d, side)
        return job, got, note

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(work, jobs))
    bid, ask = {}, {}
    status = {}
    for (d, side), got, note in results:
        (bid if side == "BID" else ask).update(got)
        status[(d, side)] = (bool(got), note)
    print("Dukascopy: %d archivos pedidos, %d velas de 1 min (BID) y %d (ASK)" % (len(jobs), len(bid), len(ask)))
    both_missing, one_missing = [], []
    for d in sorted(days):
        if d.weekday() == 5:                       # sabado: sin mercado
            continue
        okb, nb_ = status[(d, "BID")]
        oka, na_ = status[(d, "ASK")]
        if not okb and not oka:
            both_missing.append("%s (%s)" % (d.isoformat(), nb_))
        elif not okb or not oka:
            one_missing.append("%s sin %s" % (d.isoformat(), "BID" if not okb else "ASK"))
    both = sorted(ask[m]["c"] - bid[m]["c"] for m in set(bid) & set(ask))
    spread = both[len(both) // 2] if len(both) >= 100 and both[len(both) // 2] > 0 else 0.4
    print("Spread medio estimado BID/ASK: %.2f (%s)" % (spread, "medido" if len(both) >= 100 else "supuesto"))
    buckets = sorted({m.replace(minute=(m.minute // 15) * 15) for m in set(bid) | set(ask)})
    bars = []
    for t0 in buckets:
        c = duka_candle(bid, ask, t0, spread / 2)
        if c:
            bars.append(dict(t=t0, o=c["o"], h=c["h"], l=c["l"], c=c["c"], v=0.0))
    return bars, both_missing, one_missing


def compare_with_td(groups_d, bars_d, since):
    """Cuenta cuantas senales de Twelve Data coinciden con las de Dukascopy (que se parece a OANDA)."""
    try:
        raw = fetch_bars()
    except SystemExit as e:
        print("\nNo se pudo bajar Twelve Data para comparar (%s)" % e)
        return
    bars_t = clean_bars(raw, datetime.now(timezone.utc))
    groups_t = merge_signals(run_engine(bars_t, v9=False), run_engine(bars_t, v9=True))
    until = bars_d[-1]["t"] + BAR

    def conf(g, bars):
        return bars[g["i"]]["t"] + BAR

    D = [(g, conf(g, bars_d)) for g in groups_d if since <= conf(g, bars_d) <= until]
    T = [(g, conf(g, bars_t)) for g in groups_t if since <= conf(g, bars_t) <= until]
    used = set()
    matched = 0
    only_d = []
    for gd, tdt in D:
        best = None
        for k, (gt, tt) in enumerate(T):
            if k in used or gt["side"] != gd["side"]:
                continue
            dmin = abs((tt - tdt).total_seconds()) / 60.0
            if dmin <= 60 and (best is None or dmin < best[0]):
                best = (dmin, k)
        if best:
            used.add(best[1])
            matched += 1
        else:
            only_d.append((gd, tdt))
    only_t = [T[k] for k in range(len(T)) if k not in used]
    print()
    print("COMPARACION desde %s hasta %s (hora Ecuador)" % (fmt_t(since), fmt_t(until)))
    print("(misma direccion y confirmacion con menos de 1 hora de diferencia)")
    print("  Senales con datos de Dukascopy (se parecen a OANDA): %d" % len(D))
    print("  Twelve Data coincide en: %d" % matched)
    print("  Solo con Dukascopy, Twelve Data NO las avisaria: %d" % len(only_d))
    for g, t in only_d:
        print("      %s confirmada %s" % (g["side"], fmt_t(t)))
    print("  Solo con Twelve Data, avisos de mas: %d" % len(only_t))
    for g, t in only_t:
        print("      %s confirmada %s" % (g["side"], fmt_t(t)))


def run_histduka(args):
    """Corre el motor (v8 + v9) sobre velas de Dukascopy, que se parecen casi al centavo a OANDA."""
    now = datetime.now(timezone.utc)
    end_day = now.date() - timedelta(days=1)        # ultimo dia UTC completo
    raw, both_missing, one_missing = duka_bars(DUKA_DAYS, end_day)
    if one_missing:
        print("Dias con un solo lado (se ajusta con la mitad del spread): " + ", ".join(one_missing))
    if both_missing:
        print("\nDATOS INCOMPLETOS. Faltan dias enteros:")
        for b in both_missing:
            print("   " + b)
        print("No calculo senales con huecos, porque falsean el resultado.")
        print("Vuelve a correr 'histduka': lo ya descargado se guarda y solo pedira lo que falta.")
        return
    if not raw:
        sys.exit("Dukascopy no devolvio velas")
    bars = clean_bars(raw, raw[-1]["t"] + BAR)
    print("Velas de 15 min: %d | desde %s hasta %s (hora Ecuador) | modo %s"
          % (len(bars), fmt_t(bars[0]["t"]), fmt_t(bars[-1]["t"] + BAR), FREQ))
    if len(bars) < 300:
        sys.exit("Muy pocas velas utiles (%d)" % len(bars))
    print("(ignora las senales de los primeros 4-5 dias: el indicador necesita velas para calentarse)")
    groups = merge_signals(run_engine(bars, v9=False), run_engine(bars, v9=True))
    print_history(groups, bars)
    compare_with_td(groups, bars, bars[0]["t"] + timedelta(days=6))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="normal", choices=["normal", "test", "history", "velas", "feeds", "dukalive", "histduka"])
    ap.add_argument("--csv", help="(pruebas) leer velas de un CSV en vez de Twelve Data")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)

    if args.mode == "test":
        ok = send_telegram("\u2705 Bot de senales conectado. Si ves este mensaje, Telegram funciona.")
        print("Mensaje de prueba enviado" if ok else "No se pudo enviar el mensaje de prueba")
        sys.exit(0 if ok else 1)

    if args.mode == "feeds":
        run_feeds(args)
        return

    if args.mode == "dukalive":
        run_dukalive(args)
        return

    if args.mode == "histduka":
        run_histduka(args)
        return

    if args.mode == "normal" and not market_open(now):
        print("Mercado cerrado, nada que revisar.")
        return

    raw = load_csv(args.csv) if args.csv else fetch_bars()
    if args.csv and raw:
        now = raw[-1]["t"] + timedelta(days=1)
    bars = clean_bars(raw, now)
    if len(bars) < 300:
        sys.exit("Muy pocas velas utiles (%d). Revisa el feed." % len(bars))
    last_close_time = bars[-1]["t"] + BAR
    age_last = (now - last_close_time).total_seconds() / 60.0
    print("Velas: %d | ultima cerrada: %s UTC | ahora: %s UTC | atraso del feed: %.0f min | modo %s"
          % (len(bars), last_close_time.strftime("%Y-%m-%d %H:%M"),
             now.strftime("%Y-%m-%d %H:%M"), age_last, FREQ))

    groups = merge_signals(run_engine(bars, v9=False), run_engine(bars, v9=True))

    if args.mode == "history":
        print_history(groups, bars)
        return

    if args.mode == "velas":
        # Para comparar con TradingView: ultimas velas con hora de APERTURA (hora Ecuador,
        # igual que la hora que muestra TradingView al pasar el mouse) y su O/H/L/C.
        marks = {}
        for g in groups:
            for it in g["items"] + g["dups"]:
                tag = "%s %s" % (g["side"], it["ver"])
                marks.setdefault(it["label_i"], []).append("ETIQUETA " + tag)
            marks.setdefault(g["i"], []).append("aviso %s" % g["side"])
        last = bars[-VELAS_N:]
        first_i = len(bars) - len(last)
        print("Ultimas %d velas de %s (15m), hora de apertura = hora Ecuador (UTC-5):" % (len(last), SYMBOL))
        print("hora         |   apertura    maximo     minimo     cierre | marcas")
        for k, b in enumerate(last):
            m = ", ".join(marks.get(first_i + k, []))
            print("%s | %10.2f %10.2f %10.2f %10.2f | %s"
                  % (fmt_t(b["t"]), b["o"], b["h"], b["l"], b["c"], m))
        return

    n = len(bars)
    state = load_state()
    changed = False
    fresh = [g for g in groups if g["i"] >= n - LOOKBACK_BARS]
    if not fresh:
        print("Sin senales nuevas en las ultimas %d velas." % LOOKBACK_BARS)
    for s in fresh:
        key = "%s|%s" % (s["side"], bars[s["i"]]["t"].strftime("%Y-%m-%dT%H:%M"))
        if key in state["sent"]:
            print("Ya avisada: %s" % key)
            continue
        age = (now - (bars[s["i"]]["t"] + BAR)).total_seconds() / 60.0
        if age > MAX_AGE_MIN:
            print("Senal %s demasiado vieja (%.0f min), no se avisa." % (key, age))
            continue
        if send_telegram(build_message(s, bars)):
            state["sent"].append(key)
            changed = True
            print("Aviso enviado: %s" % key)
    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
