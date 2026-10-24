#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oro SMC v9 -> avisos por Telegram.
Corre en GitHub Actions cada 5 minutos. Cada ejecucion:
  1) baja ~3000 velas de XAU/USD 15m de Twelve Data,
  2) repasa TODA la logica del indicador v9 vela por vela,
  3) si una de las ultimas 2 velas cerradas disparo COMPRA/VENTA y no se
     aviso antes, manda un mensaje por Telegram.

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
def run_engine(bars):
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
            faB = CONT_FIB_A if bCont else fibA
            zTop = bHigh - faB * R
            zBot = bHigh - fibB * R
            legMin = (CONT_MIN_LEG_ATR if bCont else minLegATR) * a
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

        if ((bState == 0 or (CONT_REARM and bState == 2 and i > bMssBar)) and CONT_ON and htfBull
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
            bScore += (1 if trendUp else 0) if bCont else (1 if c < eq else 0)
            bScore += 1 if (USE_VP and poc is not None and c <= poc) else 0
            need = CONT_MIN_SCORE if bCont else MIN_SCORE
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
            faS = CONT_FIB_A if sCont else fibA
            zBotS = sLow + faS * Rs
            zTopS = sLow + fibB * Rs
            legMinS = (CONT_MIN_LEG_ATR if sCont else minLegATR) * a
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

        if ((sState == 0 or (CONT_REARM and sState == 2 and i > sMssBar)) and CONT_ON and htfBear
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
            sScore += (1 if trendDn else 0) if sCont else (1 if c > eq else 0)
            sScore += 1 if (USE_VP and poc is not None and c >= poc) else 0
            need = CONT_MIN_SCORE if sCont else MIN_SCORE
            sellSig = sScore >= need and coolOK and (not REQ_HTF or htfBear)

        # ================= Ejecucion =================
        if buySig:
            lastSig = i
            sigs.append(dict(side="COMPRA", i=i, label_i=bLowBar, close=c, sl=bSl,
                             score=bScore, model="B" if bCont else "A"))
            bState = 0
        if sellSig:
            lastSig = i
            sigs.append(dict(side="VENTA", i=i, label_i=sHighBar, close=c, sl=sSl,
                             score=sScore, model="B" if sCont else "A"))
            sState = 0

    return sigs


# ============================================================================
# Telegram / estado / mensajes
# ============================================================================
def fmt_t(t):
    return t.astimezone(EC).strftime("%d/%m %H:%M")


def build_message(sig, bars):
    b = bars[sig["i"]]
    close_time = b["t"] + BAR
    risk = abs(sig["close"] - sig["sl"])
    if sig["side"] == "COMPRA":
        tp = sig["close"] + RR * risk
        icon = "\U0001F7E2"
    else:
        tp = sig["close"] - RR * risk
        icon = "\U0001F534"
    modelo = "B (continuacion)" if sig["model"] == "B" else "A (barrido)"
    maxs = 5 if sig["model"] == "B" else 6
    return (
        "%s %s %s 15m\n"
        "Modelo %s | Puntuacion %d/%d\n"
        "Cierre de vela: %.2f\n"
        "SL: %.2f (riesgo $%.2f)\n"
        "TP 1:%g: %.2f\n"
        "Vela cerrada: %s (hora Ecuador)\n\n"
        "Precios del feed Twelve Data: pueden diferir unos dolares de TradingView. "
        "Confirma en tu grafico antes de operar."
    ) % (icon, sig["side"], SYMBOL, modelo, sig["score"], maxs, sig["close"], sig["sl"],
         risk, RR, tp, fmt_t(close_time))


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
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="normal", choices=["normal", "test", "history"])
    ap.add_argument("--csv", help="(pruebas) leer velas de un CSV en vez de Twelve Data")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)

    if args.mode == "test":
        ok = send_telegram("\u2705 Bot de senales conectado. Si ves este mensaje, Telegram funciona.")
        print("Mensaje de prueba enviado" if ok else "No se pudo enviar el mensaje de prueba")
        sys.exit(0 if ok else 1)

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

    sigs = run_engine(bars)

    if args.mode == "history":
        print("Senales en el historial (%d). Comparalas con las etiquetas de TradingView:" % len(sigs))
        for s in sigs:
            print("%-6s modelo %s punt %d | etiqueta en %s | confirmada %s | cierre %.2f SL %.2f"
                  % (s["side"], s["model"], s["score"], fmt_t(bars[s["label_i"]]["t"]),
                     fmt_t(bars[s["i"]]["t"] + BAR), s["close"], s["sl"]))
        return

    n = len(bars)
    state = load_state()
    changed = False
    fresh = [s for s in sigs if s["i"] >= n - LOOKBACK_BARS]
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
