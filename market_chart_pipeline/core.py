from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"


class ValidationError(RuntimeError):
    pass


@dataclass
class Metrics:
    ticker: str
    session_date: str
    current_price: float
    sma21: float
    sma50: float
    sma200: float
    atr14: float
    atr_pct: float
    avg_volume_50: float
    relative_volume: float
    avg_dollar_volume_50: float
    high_52w: float
    pct_from_52w_high: float
    pct_from_sma50: float
    pct_from_sma200: float
    tightness_5d_pct: float
    tightness_10d_pct: float
    tightness_15d_pct: float
    up_down_volume_ratio_50: float | None
    accumulation_days_25: int
    distribution_days_25: int
    quantitative_gate: str
    gate_reasons: list[str]


def credentials() -> tuple[str, str]:
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    if not key or not secret:
        raise ValidationError("ALPACA_API_KEY/ALPACA_API_SECRET are not configured")
    return key, secret


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    out = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if not out:
        raise ValidationError("ticker manifest is empty")
    return out


def fetch_bars(symbols: Iterable[str], session_date: str, feed: str = "iex") -> dict[str, pd.DataFrame]:
    symbols = normalize_symbols(symbols)
    key, secret = credentials()
    end = pd.Timestamp(session_date).normalize()
    start = end - pd.Timedelta(days=1100)
    params = {
        "symbols": ",".join(symbols), "timeframe": "1Day",
        "start": start.strftime("%Y-%m-%dT00:00:00Z"),
        "end": (end + pd.Timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
        "adjustment": "all", "feed": feed, "limit": 10000, "sort": "asc",
    }
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    rows = {s: [] for s in symbols}
    token = None
    while True:
        if token:
            params["page_token"] = token
        response = requests.get(DATA_URL, params=params, headers=headers, timeout=45)
        if response.status_code != 200:
            raise ValidationError(f"Alpaca request failed ({response.status_code}): {response.text[:250]}")
        payload = response.json()
        for symbol, bars in (payload.get("bars") or {}).items():
            rows.setdefault(symbol.upper(), []).extend(bars or [])
        token = payload.get("next_page_token")
        if not token:
            break
    result = {}
    for symbol, values in rows.items():
        if not values:
            continue
        df = pd.DataFrame(values).rename(columns={"t":"Date","o":"Open","h":"High","l":"Low","c":"Close","v":"Volume"})
        df["Date"] = pd.to_datetime(df["Date"], utc=True).dt.tz_convert(None).dt.normalize()
        df = df.set_index("Date")[["Open","High","Low","Close","Volume"]].sort_index()
        result[symbol] = df[~df.index.duplicated(keep="last")]
    return result


def validate_bars(symbol: str, df: pd.DataFrame, session_date: str) -> pd.DataFrame:
    end = pd.Timestamp(session_date).normalize()
    df = df.loc[df.index <= end].copy()
    if len(df) < 200:
        raise ValidationError(f"{symbol}: {len(df)} bars available; 200 required")
    if df.index[-1] != end:
        raise ValidationError(f"{symbol}: latest bar {df.index[-1].date()} != requested session {end.date()}")
    if df[["Open","High","Low","Close","Volume"]].isna().any().any():
        raise ValidationError(f"{symbol}: OHLCV contains null values")
    if (df[["Open","High","Low","Close"]] <= 0).any().any() or (df["Volume"] < 0).any():
        raise ValidationError(f"{symbol}: invalid OHLCV values")
    return df


def _tightness(df: pd.DataFrame, n: int) -> float:
    w = df.tail(n)
    return float((w.High.max() - w.Low.min()) / w.Close.iloc[-1] * 100)


def calculate_metrics(symbol: str, df: pd.DataFrame, session_date: str) -> Metrics:
    df = validate_bars(symbol, df, session_date)
    close, volume = df.Close, df.Volume
    sma21, sma50, sma200 = close.rolling(21).mean(), close.rolling(50).mean(), close.rolling(200).mean()
    prev = close.shift(1)
    tr = pd.concat([(df.High-df.Low),(df.High-prev).abs(),(df.Low-prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    avgvol = volume.rolling(50).mean()
    price = float(close.iloc[-1]); high52 = float(df.tail(252).High.max())
    change = close.pct_change(); up = volume.where(change > 0, 0).tail(50).sum(); down = volume.where(change < 0, 0).tail(50).sum()
    recent = df.tail(26); rc = recent.Close.pct_change(); pv = recent.Volume.shift(1)
    acc = int(((rc >= .002) & (recent.Volume > pv)).sum()); dist = int(((rc <= -.002) & (recent.Volume > pv)).sum())
    reasons=[]
    if float((close*volume).rolling(50).mean().iloc[-1]) < 20_000_000: reasons.append("average dollar volume below $20M")
    if price < sma200.iloc[-1]: reasons.append("below 200-day moving average")
    if price < sma50.iloc[-1]: reasons.append("below 50-day moving average")
    if (price/high52-1)*100 < -25: reasons.append("more than 25% below 52-week high")
    if (price/sma50.iloc[-1]-1)*100 > 20: reasons.append("more than 20% above 50-day moving average")
    gate = "DEPRIORITIZE" if reasons else "CHART_REVIEW"
    if not reasons:
        score=sum([price>=sma21.iloc[-1]>=sma50.iloc[-1]>=sma200.iloc[-1], (price/high52-1)*100>=-10, volume.iloc[-1]/avgvol.iloc[-1]>=1, _tightness(df,10)<=12, acc>=dist])
        if score>=3: gate="CHART_REVIEW_PRIORITY"
    return Metrics(symbol, session_date, price, float(sma21.iloc[-1]), float(sma50.iloc[-1]), float(sma200.iloc[-1]), float(atr.iloc[-1]), float(atr.iloc[-1]/price*100), float(avgvol.iloc[-1]), float(volume.iloc[-1]/avgvol.iloc[-1]), float((close*volume).rolling(50).mean().iloc[-1]), high52, float((price/high52-1)*100), float((price/sma50.iloc[-1]-1)*100), float((price/sma200.iloc[-1]-1)*100), _tightness(df,5), _tightness(df,10), _tightness(df,15), None if down<=0 else float(up/down), acc, dist, gate, reasons)


def render_chart(symbol: str, df: pd.DataFrame, session_date: str, path: Path, weekly: bool=False) -> None:
    df=validate_bars(symbol,df,session_date)
    if weekly:
        df=df.resample("W-FRI").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna().tail(156)
        mav=(10,40); title=f"{symbol} — Weekly through {session_date}"
    else:
        df=df.tail(252); mav=(21,50,200); title=f"{symbol} — Daily through {session_date}"
    path.parent.mkdir(parents=True,exist_ok=True)
    mpf.plot(df,type="candle",volume=True,mav=mav,style="yahoo",title=title,figsize=(13,7.5),tight_layout=True,savefig=dict(fname=str(path),dpi=150,bbox_inches="tight"))
    plt.close("all")
    if not path.exists() or path.stat().st_size < 5000: raise ValidationError(f"{symbol}: chart render failed")


def build_pdf(session_date: str, records: list[dict], output: Path) -> None:
    styles=getSampleStyleSheet(); story=[Paragraph(f"Market Chart Packet — {session_date}",styles["Title"]),Spacer(1,12),Paragraph(f"Verified chart records: {len(records)}",styles["BodyText"]),Spacer(1,12)]
    for i,r in enumerate(records):
        m=r["metrics"]; story.append(Paragraph(f"{m['ticker']} — {m['quantitative_gate']}",styles["Heading2"]))
        data=[["Price","21D","50D","200D","ATR%","Rel Vol","52W Dist"],[f"{m['current_price']:.2f}",f"{m['sma21']:.2f}",f"{m['sma50']:.2f}",f"{m['sma200']:.2f}",f"{m['atr_pct']:.1f}%",f"{m['relative_volume']:.2f}x",f"{m['pct_from_52w_high']:.1f}%"]]
        t=Table(data,repeatRows=1); t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("GRID",(0,0),(-1,-1),.25,colors.grey),("FONTSIZE",(0,0),(-1,-1),8)])); story.append(t); story.append(Spacer(1,8))
        story.append(Image(r["daily_chart"],width=520,height=300)); story.append(Spacer(1,8)); story.append(Image(r["weekly_chart"],width=520,height=300))
        if i < len(records)-1: story.append(PageBreak())
    SimpleDocTemplate(str(output),pagesize=letter,rightMargin=28,leftMargin=28,topMargin=28,bottomMargin=28).build(story)


def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()


def build_packet(symbols: Iterable[str], session_date: str, output_dir: Path, feed: str="iex") -> dict:
    symbols=normalize_symbols(symbols); output_dir.mkdir(parents=True,exist_ok=True); charts=output_dir/"charts"
    bars=fetch_bars(symbols,session_date,feed); records=[]; errors={}
    for symbol in symbols:
        try:
            if symbol not in bars: raise ValidationError(f"{symbol}: no bars returned")
            m=calculate_metrics(symbol,bars[symbol],session_date); daily=charts/f"{symbol}_daily.png"; weekly=charts/f"{symbol}_weekly.png"
            render_chart(symbol,bars[symbol],session_date,daily); render_chart(symbol,bars[symbol],session_date,weekly,True)
            records.append({"metrics":asdict(m),"daily_chart":str(daily),"weekly_chart":str(weekly)})
        except Exception as exc: errors[symbol]=str(exc)
    payload={"session_date":session_date,"requested_tickers":symbols,"verified_count":len(records),"error_count":len(errors),"errors":errors,"records":records,"status":"COMPLETE" if not errors else "COMPLETE_WITH_WARNINGS"}
    json_path=output_dir/f"Market_Chart_Data_{session_date}.json"; json_path.write_text(json.dumps(payload,indent=2),encoding="utf-8")
    pdf_path=output_dir/f"Market_Chart_Packet_{session_date}.pdf"; build_pdf(session_date,records,pdf_path)
    payload["artifacts"]={"json":str(json_path),"pdf":str(pdf_path),"pdf_sha256":sha256(pdf_path)}
    json_path.write_text(json.dumps(payload,indent=2),encoding="utf-8")
    return payload
