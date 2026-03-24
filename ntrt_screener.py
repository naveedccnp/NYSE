#!/usr/bin/env python3
"""
NTRT/MTRT Daily Screener — US Markets
Multi-source: yfinance (primary) + Yahoo Finance direct + FMP (optional)
Pre/post movers: Yahoo screener → StockAnalysis → yfinance trending fallback

Usage:
  pip install yfinance requests
  python ntrt_screener.py [--fmp-key KEY] [--date YYYY-MM-DD] [--demo]
"""
import os, sys, json, time, argparse, datetime, requests, re
from typing import Optional

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    print("[warn] yfinance not installed — pip install yfinance")

DATA_FILE   = "ntrt_data.json"
MAX_HISTORY = 60

# MAGNA53 thresholds
MAGNA_G_GAP      = 4.0
MAGNA_G_VOL      = 100_000
MAGNA_A_REV      = 25.0
MOVER_MIN_PRICE  = 2.00
MOVER_MIN_VOL    = 100_000
MOVER_PCT        = 4.0
MOVER_DOLLAR     = 5.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json,*/*",
}

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--fmp-key", default=os.environ.get("FMP_API_KEY",""))
    p.add_argument("--date",    default="")
    p.add_argument("--demo",    action="store_true")
    return p.parse_args()

def today_str(override=""):
    return override or datetime.date.today().isoformat()

def is_weekday(d):
    return datetime.date.fromisoformat(d).weekday() < 5

def fvol(v):
    if v is None: return "—"
    if v >= 1e6:  return f"{v/1e6:.1f}M"
    if v >= 1e3:  return f"{v/1e3:.0f}K"
    return str(int(v))

# ── Quote helpers ─────────────────────────────────────────────────────────────
def get_quote_yf(ticker):
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        p    = info.get("regularMarketPrice") or info.get("currentPrice")
        if not p: return {}
        return {
            "price":       p,
            "prev_close":  info.get("regularMarketPreviousClose") or info.get("previousClose"),
            "volume":      info.get("regularMarketVolume") or info.get("volume"),
            "market_cap":  info.get("marketCap"),
            "name":        info.get("longName") or info.get("shortName",""),
            "pre_price":   info.get("preMarketPrice"),
            "pre_chg":     info.get("preMarketChange"),
            "pre_pct":     info.get("preMarketChangePercent"),
            "post_price":  info.get("postMarketPrice"),
            "post_chg":    info.get("postMarketChange"),
            "post_pct":    info.get("postMarketChangePercent"),
        }
    except Exception as e:
        print(f"    [yf] {ticker}: {e}")
        return {}

def get_quote_v8(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        r   = requests.get(url, headers=HEADERS, timeout=10)
        m   = r.json().get("chart",{}).get("result",[{}])[0].get("meta",{})
        p   = m.get("regularMarketPrice")
        if not p: return {}
        return {
            "price":      p,
            "prev_close": m.get("chartPreviousClose") or m.get("previousClose"),
            "volume":     m.get("regularMarketVolume"),
            "market_cap": m.get("marketCap"),
        }
    except Exception as e:
        print(f"    [v8] {ticker}: {e}")
        return {}

def get_income_yf(ticker):
    try:
        t  = yf.Ticker(ticker)
        qf = t.quarterly_financials
        af = t.financials
        out = {}
        if not qf.empty and "Total Revenue" in qf.index:
            rv = qf.loc["Total Revenue"].dropna().tolist()
            if len(rv) >= 4 and rv[3]:
                out["rev_growth"]    = round((rv[0]-rv[3])/abs(rv[3])*100,1)
            if len(rv) >= 5 and rv[4]:
                out["rev_growth_q2"] = round((rv[1]-rv[4])/abs(rv[4])*100,1)
        if not af.empty and "Total Revenue" in af.index:
            ar = af.loc["Total Revenue"].iloc[0]
            if ar: out["annual_revenue"] = float(ar)
        eh = t.earnings_history
        if eh is not None and not eh.empty and len(eh) >= 4:
            e0 = eh.iloc[-1].get("epsActual")
            e3 = eh.iloc[-4].get("epsActual")
            if e0 and e3 and e3 != 0:
                out["eps_growth"] = round((e0-e3)/abs(e3)*100,1)
        return out
    except Exception as e:
        print(f"    [yf_income] {ticker}: {e}")
        return {}

def get_income_v10(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=incomeStatementHistoryQuarterly,earningsHistory"
        r   = requests.get(url, headers=HEADERS, timeout=12)
        d   = r.json().get("quoteSummary",{}).get("result",[{}])[0]
        out = {}
        qh  = d.get("incomeStatementHistoryQuarterly",{}).get("incomeStatementHistory",[])
        rv  = [q.get("totalRevenue",{}).get("raw") for q in qh[:6] if q.get("totalRevenue",{}).get("raw")]
        if len(rv) >= 4 and rv[3]: out["rev_growth"]    = round((rv[0]-rv[3])/abs(rv[3])*100,1)
        if len(rv) >= 5 and rv[4]: out["rev_growth_q2"] = round((rv[1]-rv[4])/abs(rv[4])*100,1)
        eh = d.get("earningsHistory",{}).get("history",[])
        if len(eh) >= 4:
            e0=(eh[0].get("epsActual") or {}).get("raw")
            e3=(eh[3].get("epsActual") or {}).get("raw")
            if e0 and e3 and e3!=0: out["eps_growth"] = round((e0-e3)/abs(e3)*100,1)
        return out
    except Exception as e:
        print(f"    [v10] {ticker}: {e}")
        return {}

# ── Pre/post movers — multi-source ────────────────────────────────────────────
def _qualifies(price, vol, chgpct, chgabs):
    if not price or price < MOVER_MIN_PRICE: return False
    if not vol   or vol   < MOVER_MIN_VOL:   return False
    return abs(chgpct or 0) >= MOVER_PCT or abs(chgabs or 0) >= MOVER_DOLLAR

def get_movers_yahoo():
    movers = []
    for scrId, session, is_pre in [
        ("pm_gainers","Pre-Market",True), ("pm_losers","Pre-Market",True),
        ("ah_gainers","Post-Market",False),("ah_losers","Post-Market",False),
    ]:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds={scrId}&count=50"
            r   = requests.get(url, headers={**HEADERS,"Referer":"https://finance.yahoo.com/"}, timeout=15)
            if r.status_code != 200: continue
            quotes = r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[])
            print(f"  [yahoo:{scrId}] {len(quotes)} quotes")
            for q in quotes:
                sym = q.get("symbol","")
                if not sym: continue
                sp  = q.get("preMarketPrice"    if is_pre else "postMarketPrice")
                sc  = q.get("preMarketChange"   if is_pre else "postMarketChange")
                spc = q.get("preMarketChangePercent" if is_pre else "postMarketChangePercent")
                sv  = q.get("preMarketVolume"   if is_pre else "postMarketVolume") or q.get("regularMarketVolume",0)
                rp  = q.get("regularMarketPrice",0)
                if not _qualifies(sp or rp, sv, spc, sc): continue
                movers.append(dict(ticker=sym,company=q.get("longName",""),
                    session=session,session_price=sp,session_chg=sc,session_chgpct=spc,
                    session_vol=sv,reg_price=rp,market_cap=q.get("marketCap"),
                    passes_pct=abs(spc or 0)>=MOVER_PCT,passes_dol=abs(sc or 0)>=MOVER_DOLLAR))
        except Exception as e:
            print(f"  [yahoo:{scrId}] {e}")
        time.sleep(0.3)
    return movers

def get_movers_stockanalysis():
    movers = []
    for url, session in [
        ("https://stockanalysis.com/markets/pre-market/gainers/","Pre-Market"),
        ("https://stockanalysis.com/markets/pre-market/losers/","Pre-Market"),
        ("https://stockanalysis.com/markets/after-hours/gainers/","Post-Market"),
        ("https://stockanalysis.com/markets/after-hours/losers/","Post-Market"),
    ]:
        try:
            r = requests.get(url, headers={**HEADERS,"Referer":"https://stockanalysis.com/"}, timeout=15)
            if r.status_code != 200: continue
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', r.text)
            if not m: continue
            nd   = json.loads(m.group(1))
            pp   = nd.get("props",{}).get("pageProps",{})
            rows = pp.get("data",[]) or pp.get("stocks",[])
            print(f"  [stockanalysis:{session}] {len(rows)} rows")
            for row in rows:
                sym    = row.get("s","") or row.get("symbol","")
                price  = row.get("p") or row.get("price")
                chgpct = row.get("c") or row.get("changePercent",0)
                chgabs = row.get("change",0)
                vol    = row.get("v") or row.get("volume",0)
                if not sym or not _qualifies(price, vol, chgpct, chgabs): continue
                movers.append(dict(ticker=sym,company=row.get("n","") or row.get("name",""),
                    session=session,session_price=price,session_chg=chgabs,session_chgpct=chgpct,
                    session_vol=vol,reg_price=price,market_cap=row.get("marketCap"),
                    passes_pct=abs(chgpct or 0)>=MOVER_PCT,passes_dol=abs(chgabs or 0)>=MOVER_DOLLAR))
        except Exception as e:
            print(f"  [stockanalysis] {e}")
        time.sleep(0.5)
    return movers

def get_movers_yf_trending():
    if not HAS_YF: return []
    movers = []
    try:
        syms = []
        r = requests.get("https://query2.finance.yahoo.com/v1/finance/trending/US?count=50", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            syms = [q.get("symbol","") for q in r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[]) if q.get("symbol")]
        for sym in syms[:30]:
            try:
                info = yf.Ticker(sym).info
                rp   = info.get("regularMarketPrice",0) or info.get("currentPrice",0)
                vol  = info.get("volume") or info.get("regularMarketVolume",0)
                for sess, sp, sc, spc in [
                    ("Pre-Market",  info.get("preMarketPrice"),  info.get("preMarketChange"),  info.get("preMarketChangePercent")),
                    ("Post-Market", info.get("postMarketPrice"), info.get("postMarketChange"), info.get("postMarketChangePercent")),
                ]:
                    if not _qualifies(sp, vol, spc, sc): continue
                    movers.append(dict(ticker=sym,company=info.get("longName",""),
                        session=sess,session_price=sp,session_chg=sc,session_chgpct=spc,
                        session_vol=vol,reg_price=rp,market_cap=info.get("marketCap"),
                        passes_pct=abs(spc or 0)>=MOVER_PCT,passes_dol=abs(sc or 0)>=MOVER_DOLLAR))
                time.sleep(0.2)
            except: pass
    except Exception as e:
        print(f"  [yf_trending] {e}")
    return movers

def get_all_movers():
    print("\n  Scanning pre/post market sources...")
    result, seen = [], set()
    def add(lst):
        for m in lst:
            if m.get("ticker") not in seen:
                result.append(m); seen.add(m["ticker"])

    m1 = get_movers_yahoo();         print(f"  Yahoo:         {len(m1)} qualifying"); add(m1)
    if len(result) < 3:
        m2 = get_movers_stockanalysis(); print(f"  StockAnalysis: {len(m2)} qualifying"); add(m2)
    if len(result) < 5 and HAS_YF:
        m3 = get_movers_yf_trending();   print(f"  YF trending:   {len(m3)} qualifying"); add(m3)
    print(f"  Total unique movers: {len(result)}")
    return result

# ── Earnings calendar ─────────────────────────────────────────────────────────
def get_earnings_yahoo(date):
    try:
        r = requests.get("https://query2.finance.yahoo.com/v2/finance/calendar/earnings",
            params={"date":date,"size":200,"offset":0},
            headers={**HEADERS,"Referer":"https://finance.yahoo.com/calendar/earnings"}, timeout=15)
        if r.status_code != 200: return []
        rows = r.json().get("earnings",{}).get("rows",[])
        out  = []
        for row in rows:
            sym = row.get("ticker","")
            if not sym or len(sym)>5 or "." in sym: continue
            t = row.get("startdatetimetype","TNS")
            timing = "AMC" if t=="afterHours" else "BMO" if t=="beforeHours" else "TNS"
            out.append(dict(ticker=sym,company=row.get("companyshortname",""),timing=timing,
                eps_est=row.get("epsestimate"),eps_act=row.get("epsactual"),
                rev_est=row.get("revenueestimate"),rev_act=row.get("revenueactual")))
        print(f"  [earn_yahoo] {len(out)} for {date}")
        return out
    except Exception as e:
        print(f"  [earn_yahoo] {e}"); return []

def get_earnings_fmp(date, key):
    if not key: return []
    try:
        r = requests.get("https://financialmodelingprep.com/api/v3/earning_calendar",
            params={"from":date,"to":date,"apikey":key}, headers=HEADERS, timeout=12)
        if r.status_code != 200: return []
        out = []
        for item in r.json():
            sym = item.get("symbol","")
            if not sym or len(sym)>5 or "." in sym: continue
            tr = item.get("time","").lower()
            timing = "AMC" if "amc" in tr else "BMO" if "bmo" in tr else "TNS"
            out.append(dict(ticker=sym,company=item.get("name",""),timing=timing,
                eps_est=item.get("epsEstimated"),eps_act=item.get("eps"),
                rev_est=item.get("revenueEstimated"),rev_act=item.get("revenue")))
        print(f"  [earn_fmp] {len(out)} for {date}")
        return out
    except Exception as e:
        print(f"  [earn_fmp] {e}"); return []

def get_earnings(date, fmp_key):
    tickers = get_earnings_fmp(date, fmp_key)
    if not tickers: tickers = get_earnings_yahoo(date)
    return tickers

# ── MAGNA53 scoring ────────────────────────────────────────────────────────────
def surprise(actual, est):
    if actual is None or est is None or est == 0: return None
    return round((actual-est)/abs(est)*100,1)

def score(r):
    # M
    mlist = []
    if r.get("eps_growth") and abs(r["eps_growth"]) >= 100:   mlist.append(f"EPS growth {r['eps_growth']:+.0f}%")
    if r.get("rev_growth") and r["rev_growth"]     >= 100:   mlist.append(f"Rev growth {r['rev_growth']:+.0f}%")
    if r.get("eps_surprise") and abs(r["eps_surprise"]) >= 100: mlist.append(f"EPS surprise {r['eps_surprise']:+.0f}%")
    if r.get("rev_growth",0)>=29 and r.get("rev_growth_q2",0) and r["rev_growth_q2"]>=29: mlist.append("2Q rev ≥29%")
    r["magna_m"] = bool(mlist); r["magna_m_detail"] = " · ".join(mlist) or "No massive metric"

    # G
    g,v = r.get("gap_pct"), r.get("volume")
    r["magna_g"] = bool(g and abs(g)>=MAGNA_G_GAP and v and v>=MAGNA_G_VOL)
    r["magna_g_detail"] = f"Gap {g:+.1f}%, Vol {fvol(v)}" if g else "Gap data pending"

    # N
    nlist = []
    mc = r.get("market_cap")
    if mc:
        if mc < 500_000_000: nlist.append("Small-cap (<$500M)")
        elif mc < 2_000_000_000: nlist.append("Mid-cap (<$2B)")
    if r.get("gap_pct") and abs(r["gap_pct"]) >= 15: nlist.append("Large gap signals neglect")
    if r.get("inst_holders") and r["inst_holders"] < 30: nlist.append(f"Low institutions ({r['inst_holders']})")
    r["magna_n"] = bool(nlist)
    r["neglect_strength"] = "strong" if len(nlist)>=2 else "partial" if nlist else "none"
    r["magna_n_detail"] = " · ".join(nlist) or "Large-cap — neglect unlikely"

    # A
    rg, rg2 = r.get("rev_growth"), r.get("rev_growth_q2")
    r["magna_a"] = bool(rg and (rg>=MAGNA_A_REV or (rg>=29 and rg2 and rg2>=29)))
    r["magna_a_detail"] = (f"Rev growth: {rg:+.1f}% (prev Q: {rg2:+.1f}%)" if rg else "Rev data unavailable")

    # 3 (analyst upgrades — needs FMP)
    if not r.get("magna_3"):
        r["magna_3"] = False; r["magna_3_detail"] = r.get("magna_3_detail","Analyst data needs FMP key")

    r["magna_score"] = sum(1 for k in ["magna_m","magna_g","magna_n","magna_a","magna_3"] if r.get(k))

    # Setup types
    st = []
    if r.get("gap_pct",0)>=4 and (r.get("volume") or 0)>=100000 and (r.get("rev_growth") or 0)>=29 and r["magna_n"]:
        st.append("A"); r["setup_a"]=True
    if ((abs(r.get("eps_growth") or 0)>=100 or (r.get("rev_growth") or 0)>=100 or abs(r.get("eps_surprise") or 0)>=100)
            and (r.get("rev_growth") or 0)>=10 and r["magna_n"]):
        st.append("B"); r["setup_b"]=True
    if (abs(r.get("eps_surprise") or 0)>=100 and (r.get("rev_growth") or 0)>=10
            and (r.get("annual_revenue") or 0)>=25e6 and r["magna_n"]):
        st.append("C"); r["setup_c"]=True
    r["setup_types"] = st

    sc = r["magna_score"]
    if r.get("verdict") != "MOVER":
        r["verdict"] = "STRONG" if sc>=4 and st else "WATCH" if sc>=3 and st else "MONITOR" if sc>=2 else "SKIP"

    # Story
    parts = []
    if r.get("eps_surprise"): parts.append(f"EPS surprise: {r['eps_surprise']:+.0f}%")
    if r.get("rev_growth"):   parts.append(f"Rev: {r['rev_growth']:+.1f}% YoY")
    if r.get("gap_pct"):      parts.append(f"Gap: {r['gap_pct']:+.1f}%")
    if r.get("is_new_criteria_mover"):
        t=[]
        if r.get("new_criteria_passes_pct"): t.append(f"≥4% ({r.get('session_chgpct') or 0:+.1f}%)")
        if r.get("new_criteria_passes_dol"): t.append(f"≥$5 (${abs(r.get('session_chg') or 0):.2f})")
        parts.append("🚀 " + " & ".join(t))
    r["story"] = " · ".join(parts) or "Insufficient data"
    return r

def analyse_ticker(info, fmp_key, date):
    sym = info["ticker"]
    print(f"  → {sym} ({info.get('timing','?')}) ...", end=" ", flush=True)
    time.sleep(0.3)

    r = dict(ticker=sym, company=info.get("company",sym), timing=info.get("timing","TNS"),
        date=date, price=None, prev_close=None, gap_pct=None, volume=None, market_cap=None,
        eps_actual=info.get("eps_act"), eps_estimate=info.get("eps_est"),
        eps_surprise=None, eps_growth=None,
        rev_actual=info.get("rev_act"), rev_estimate=info.get("rev_est"),
        rev_growth=None, rev_growth_q2=None, annual_revenue=None,
        magna_m=False, magna_m_detail="", magna_g=False, magna_g_detail="",
        magna_n=False, magna_n_detail="", magna_a=False, magna_a_detail="",
        magna_3=False, magna_3_detail="", magna_score=0,
        setup_a=False, setup_b=False, setup_c=False, setup_types=[],
        analyst_upgrades=0, short_interest=None, inst_holders=None,
        story="", verdict="MONITOR", neglect_strength="none",
        is_new_criteria_mover=False, new_criteria_passes_pct=False, new_criteria_passes_dol=False)

    q = (get_quote_yf(sym) if HAS_YF else {}) or get_quote_v8(sym)
    if q:
        r["price"]      = q.get("price")
        r["prev_close"] = q.get("prev_close")
        r["volume"]     = q.get("volume")
        r["market_cap"] = q.get("market_cap")
        if r["price"] and r["prev_close"] and r["prev_close"] > 0:
            r["gap_pct"] = round((r["price"]-r["prev_close"])/r["prev_close"]*100,2)

    r["eps_surprise"] = surprise(r["eps_actual"], r["eps_estimate"])

    if r["price"] and r["price"]>=MOVER_MIN_PRICE and (r.get("volume") or 0)>=MOVER_MIN_VOL:
        pct_ok  = r["gap_pct"] and abs(r["gap_pct"])>=MOVER_PCT
        dol_ok  = r["price"] and r["prev_close"] and abs(r["price"]-r["prev_close"])>=MOVER_DOLLAR
        r["new_criteria_passes_pct"] = bool(pct_ok)
        r["new_criteria_passes_dol"] = bool(dol_ok)
        r["is_new_criteria_mover"]   = bool(pct_ok or dol_ok)

    time.sleep(0.3)
    inc = (get_income_yf(sym) if HAS_YF else {}) or get_income_v10(sym)
    r["rev_growth"]     = inc.get("rev_growth")
    r["rev_growth_q2"]  = inc.get("rev_growth_q2")
    r["annual_revenue"] = inc.get("annual_revenue")
    r["eps_growth"]     = inc.get("eps_growth")

    result = score(r)
    print(result["verdict"])
    return result

def enrich_mover(m, date):
    r = dict(ticker=m["ticker"], company=m.get("company",""), timing=m.get("session",""),
        date=date, price=m.get("session_price") or m.get("reg_price"),
        prev_close=m.get("reg_price"), gap_pct=m.get("session_chgpct"),
        volume=m.get("session_vol"), market_cap=m.get("market_cap"),
        eps_actual=None, eps_estimate=None, eps_surprise=None, eps_growth=None,
        rev_actual=None, rev_estimate=None, rev_growth=None, rev_growth_q2=None, annual_revenue=None,
        magna_m=False, magna_m_detail="Mover — no earnings data today",
        magna_g=False, magna_g_detail="", magna_n=False, magna_n_detail="",
        magna_a=False, magna_a_detail="", magna_3=False, magna_3_detail="",
        magna_score=0, setup_a=False, setup_b=False, setup_c=False, setup_types=[],
        analyst_upgrades=0, short_interest=None, inst_holders=None,
        story="", verdict="MOVER", neglect_strength="none",
        session=m.get("session",""), session_chgpct=m.get("session_chgpct"),
        session_chg=m.get("session_chg"),
        is_new_criteria_mover=True,
        new_criteria_passes_pct=m.get("passes_pct",False),
        new_criteria_passes_dol=m.get("passes_dol",False))
    return score(r)

# ── History I/O ───────────────────────────────────────────────────────────────
def load_history():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f: return json.load(f)
    return []

def save_history(history):
    history = history[-MAX_HISTORY:]
    with open(DATA_FILE,"w") as f: json.dump(history,f,indent=2)
    print(f"  Saved → {DATA_FILE} ({len(history)} days)")

# ── Demo data ─────────────────────────────────────────────────────────────────
def make_demo(date):
    return {"date":date,"scanned_at":datetime.datetime.utcnow().isoformat()+"Z",
        "market":"DEMO","total_earnings":14,"total_screened":5,"pre_post_movers_count":3,
        "candidates":[
            {"ticker":"FSLY","company":"Fastly Inc.","timing":"AMC","date":date,
             "price":12.45,"prev_close":8.90,"gap_pct":39.9,"volume":42000000,"market_cap":1650000000,
             "eps_actual":0.12,"eps_estimate":0.06,"eps_surprise":100.0,"eps_growth":None,
             "rev_actual":172600000,"rev_estimate":160000000,"rev_growth":23.0,"rev_growth_q2":18.5,"annual_revenue":680000000,
             "magna_m":True,"magna_m_detail":"EPS surprise +100%","magna_g":True,"magna_g_detail":"Gap +39.9%, Vol 42.0M",
             "magna_n":True,"magna_n_detail":"Mid-cap (<$2B)","magna_a":True,"magna_a_detail":"Rev growth: +23.0%",
             "magna_3":True,"magna_3_detail":"William Blair upgraded to Buy","magna_score":5,
             "setup_a":True,"setup_b":True,"setup_c":True,"setup_types":["B","C"],
             "analyst_upgrades":2,"short_interest":None,"inst_holders":None,
             "story":"EPS surprise: +100% · Rev: +23.0% · Gap: +39.9% · 🚀 ≥4% (+39.9%) & ≥$5",
             "verdict":"STRONG","neglect_strength":"strong",
             "is_new_criteria_mover":True,"new_criteria_passes_pct":True,"new_criteria_passes_dol":True,
             "session":"Post-Market","session_chgpct":39.9,"session_chg":3.55},
            {"ticker":"PMOV","company":"Pre-Market Mover Corp","timing":"Pre-Market","date":date,
             "price":8.40,"prev_close":5.30,"gap_pct":58.5,"volume":3200000,"market_cap":310000000,
             "eps_actual":None,"eps_estimate":None,"eps_surprise":None,"eps_growth":None,
             "rev_actual":None,"rev_estimate":None,"rev_growth":None,"rev_growth_q2":None,"annual_revenue":None,
             "magna_m":False,"magna_m_detail":"No earnings data","magna_g":True,"magna_g_detail":"Gap +58.5%, Vol 3.2M",
             "magna_n":True,"magna_n_detail":"Small-cap · Large gap","magna_a":False,"magna_a_detail":"No revenue data",
             "magna_3":False,"magna_3_detail":"","magna_score":2,
             "setup_a":False,"setup_b":False,"setup_c":False,"setup_types":[],
             "analyst_upgrades":0,"short_interest":None,"inst_holders":None,
             "story":"UP 58.5% Pre-Market · $3.10 move · Vol 3.2M · 🚀 ≥4% & ≥$5",
             "verdict":"MOVER","neglect_strength":"strong",
             "is_new_criteria_mover":True,"new_criteria_passes_pct":True,"new_criteria_passes_dol":True,
             "session":"Pre-Market","session_chgpct":58.5,"session_chg":3.10},
            {"ticker":"ASTH","company":"Asthma Holdings Inc.","timing":"BMO","date":date,
             "price":6.40,"prev_close":4.80,"gap_pct":33.3,"volume":8500000,"market_cap":1100000000,
             "eps_actual":0.22,"eps_estimate":0.11,"eps_surprise":108.0,"eps_growth":220.0,
             "rev_actual":None,"rev_estimate":None,"rev_growth":43.0,"rev_growth_q2":38.0,"annual_revenue":180000000,
             "magna_m":True,"magna_m_detail":"EPS surprise +108% · EPS growth +220%","magna_g":True,"magna_g_detail":"Gap +33.3%, Vol 8.5M",
             "magna_n":True,"magna_n_detail":"Small-cap · Low institutional coverage",
             "magna_a":True,"magna_a_detail":"Rev growth: +43.0%","magna_3":False,"magna_3_detail":"<3 upgrades",
             "magna_score":4,"setup_a":True,"setup_b":True,"setup_c":False,"setup_types":["A","B"],
             "analyst_upgrades":1,"short_interest":6.2,"inst_holders":18,
             "story":"EPS surprise: +108% · Rev: +43.0% · Gap: +33.3% · 🚀 ≥4% & ≥$5",
             "verdict":"STRONG","neglect_strength":"strong",
             "is_new_criteria_mover":True,"new_criteria_passes_pct":True,"new_criteria_passes_dol":True,
             "session":"Pre-Market","session_chgpct":33.3,"session_chg":1.60},
            {"ticker":"AVGO","company":"Broadcom Inc.","timing":"AMC","date":date,
             "price":214.50,"prev_close":204.90,"gap_pct":4.7,"volume":22000000,"market_cap":1000000000000,
             "eps_actual":1.60,"eps_estimate":1.48,"eps_surprise":8.0,"eps_growth":29.0,
             "rev_actual":19300000000,"rev_estimate":18900000000,"rev_growth":29.0,"rev_growth_q2":25.0,"annual_revenue":72000000000,
             "magna_m":True,"magna_m_detail":"AI revenue +106% YoY","magna_g":True,"magna_g_detail":"Gap +4.7%, Vol 22.0M",
             "magna_n":False,"magna_n_detail":"$1T market cap","magna_a":True,"magna_a_detail":"Rev growth: +29.0%",
             "magna_3":True,"magna_3_detail":"Multiple analyst upgrades","magna_score":4,
             "setup_a":False,"setup_b":False,"setup_c":False,"setup_types":["B"],
             "analyst_upgrades":4,"short_interest":None,"inst_holders":None,
             "story":"Rev: +29.0% · Gap: +4.7% · 🚀 ≥4% & ≥$5",
             "verdict":"WATCH","neglect_strength":"none",
             "is_new_criteria_mover":True,"new_criteria_passes_pct":True,"new_criteria_passes_dol":True,
             "session":"Post-Market","session_chgpct":4.7,"session_chg":9.60},
            {"ticker":"KEYS","company":"Keysight Technologies","timing":"BMO","date":date,
             "price":148.50,"prev_close":131.90,"gap_pct":12.6,"volume":5200000,"market_cap":24500000000,
             "eps_actual":2.12,"eps_estimate":2.02,"eps_surprise":5.0,"eps_growth":45.0,
             "rev_actual":1350000000,"rev_estimate":1320000000,"rev_growth":10.0,"rev_growth_q2":8.5,"annual_revenue":5200000000,
             "magna_m":False,"magna_m_detail":"EPS surprise +5% — below threshold","magna_g":True,"magna_g_detail":"Gap +12.6%, Vol 5.2M",
             "magna_n":False,"magna_n_detail":"Large-cap ($24B)","magna_a":False,"magna_a_detail":"Rev +10.0% — below 25%",
             "magna_3":True,"magna_3_detail":"Multiple analyst upgrades","magna_score":2,
             "setup_a":False,"setup_b":False,"setup_c":False,"setup_types":[],
             "analyst_upgrades":2,"short_interest":None,"inst_holders":None,
             "story":"EPS surprise: +5% · Rev: +10.0% · Gap: +12.6% · 🚀 ≥4% & ≥$5",
             "verdict":"MONITOR","neglect_strength":"none",
             "is_new_criteria_mover":True,"new_criteria_passes_pct":True,"new_criteria_passes_dol":True,
             "session":"Pre-Market","session_chgpct":12.6,"session_chg":16.60}
        ]}

# ── Main ──────────────────────────────────────────────────────────────────────
def run_scan(date, fmp_key):
    print(f"\n=== NTRT/MTRT + New Criteria Scan: {date} ===")
    if not is_weekday(date):
        print("  Weekend — skipping."); return None

    # Earnings
    tickers = get_earnings(date, fmp_key)
    print(f"  {len(tickers)} earnings reports found")
    candidates = []
    for info in tickers[:80]:
        try:
            r = analyse_ticker(info, fmp_key, date)
            if any(r.get(k) for k in ["gap_pct","rev_growth","eps_surprise"]):
                candidates.append(r)
        except Exception as e:
            print(f"  [err] {info['ticker']}: {e}")
        time.sleep(0.5)

    # Movers
    movers = get_all_movers()
    seen   = {c["ticker"] for c in candidates}
    extra  = []
    for m in movers[:40]:
        if m["ticker"] in seen:
            for c in candidates:
                if c["ticker"] == m["ticker"]:
                    c["is_new_criteria_mover"]   = True
                    c["new_criteria_passes_pct"] = m.get("passes_pct",False)
                    c["new_criteria_passes_dol"] = m.get("passes_dol",False)
                    c["session"]     = m.get("session","")
                    c["session_chgpct"] = m.get("session_chgpct")
                    c["session_chg"]    = m.get("session_chg")
        else:
            try: extra.append(enrich_mover(m, date))
            except Exception as e: print(f"  [mover] {m['ticker']}: {e}")
        time.sleep(0.2)

    all_c = candidates + extra
    order = {"STRONG":0,"WATCH":1,"MOVER":2,"MONITOR":3,"SKIP":4}
    all_c.sort(key=lambda x:(order.get(x.get("verdict",""),9),-x.get("magna_score",0)))

    strong = sum(1 for c in all_c if c.get("verdict")=="STRONG")
    movc   = sum(1 for c in all_c if c.get("is_new_criteria_mover"))
    print(f"\n  Candidates: {len(all_c)} | STRONG: {strong} | Movers: {movc}")

    return {"date":date,"scanned_at":datetime.datetime.utcnow().isoformat()+"Z",
        "market":"US","total_earnings":len(tickers),"total_screened":len(candidates),
        "pre_post_movers_count":len(extra),"candidates":all_c}

def main():
    args = parse_args()
    date = today_str(args.date)
    if args.demo:
        print(f"[demo] Generating demo for {date}")
        h = load_history()
        h = [x for x in h if x.get("date") != date]
        h.append(make_demo(date))
        save_history(h); return
    result = run_scan(date, args.fmp_key)
    if result is None: return
    h = load_history()
    h = [x for x in h if x.get("date") != date]
    h.append(result)
    save_history(h)
    print("✓ Done.")

if __name__ == "__main__":
    main()
