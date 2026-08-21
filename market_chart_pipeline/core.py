from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .candidate_charts import render_pattern_chart
from .patterns import PATTERN_ALGORITHM_VERSION
from .fmp import enrich_symbol, historical_eod, history_concurrency, price_history_metadata
from .technicals import calculate_technical_context


class ValidationError(RuntimeError):
    pass


@dataclass
class Metrics:
    ticker: str
    session_date: str
    asset_class: str
    current_price: float
    sma21: float
    sma50: float
    sma200: float
    atr14: float
    atr_pct: float
    avg_volume_50: float | None
    relative_volume: float | None
    avg_dollar_volume_50: float | None
    high_52w: float
    pct_from_52w_high: float
    pct_from_sma50: float
    pct_from_sma200: float
    tightness_5d_pct: float
    tightness_10d_pct: float
    tightness_15d_pct: float
    up_down_volume_ratio_50: float | None
    accumulation_days_25: int | None
    distribution_days_25: int | None
    quantitative_gate: str
    gate_reasons: list[str]


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    out = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if not out:
        raise ValidationError("ticker manifest is empty")
    return out


def fetch_bars(
    symbols: Iterable[str], session_date: str
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    symbols = normalize_symbols(symbols)
    end = pd.Timestamp(session_date).normalize()
    start = end - pd.Timedelta(days=1100)
    fetched: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=history_concurrency()) as executor:
        futures = {
            executor.submit(
                historical_eod,
                symbol,
                start.date().isoformat(),
                end.date().isoformat(),
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if frame.empty:
                    raise ValidationError(f"{symbol}: FMP returned no historical bars")
                fetched[symbol] = frame
            except Exception as exc:
                errors[symbol] = str(exc)
    return (
        {symbol: fetched[symbol] for symbol in symbols if symbol in fetched},
        {symbol: errors[symbol] for symbol in symbols if symbol in errors},
    )


def validate_bars(symbol: str, df: pd.DataFrame, session_date: str) -> pd.DataFrame:
    end = pd.Timestamp(session_date).normalize()
    df = df.loc[df.index <= end].copy()
    if len(df) < 200:
        raise ValidationError(f"{symbol}: {len(df)} bars available; 200 required")
    if df.index[-1] != end:
        raise ValidationError(f"{symbol}: latest bar {df.index[-1].date()} != requested session {end.date()}")
    if df[["Open","High","Low","Close"]].isna().any().any():
        raise ValidationError(f"{symbol}: OHLC contains null values")
    if (df[["Open","High","Low","Close"]] <= 0).any().any():
        raise ValidationError(f"{symbol}: invalid OHLC values")
    asset_class = str(df.attrs.get("asset_class") or "EQUITY")
    volume_missing = df["Volume"].isna().any()
    if asset_class != "FOREX" and volume_missing:
        raise ValidationError(f"{symbol}: equity volume contains null values")
    if df["Volume"].dropna().lt(0).any():
        raise ValidationError(f"{symbol}: volume contains negative values")
    if asset_class != "FOREX" and df["Volume"].le(0).any():
        raise ValidationError(f"{symbol}: equity volume must be positive for every session")
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
    volume_available = not volume.isna().any()
    avgvol = volume.rolling(50).mean() if volume_available else pd.Series(index=df.index, dtype=float)
    price = float(close.iloc[-1]); high52 = float(df.tail(252).High.max())
    change = close.pct_change()
    if volume_available:
        up = volume.where(change > 0, 0).tail(50).sum(); down = volume.where(change < 0, 0).tail(50).sum()
        recent = df.tail(26); rc = recent.Close.pct_change(); pv = recent.Volume.shift(1)
        acc = int(((rc >= .002) & (recent.Volume > pv)).sum()); dist = int(((rc <= -.002) & (recent.Volume > pv)).sum())
        avg_volume_50 = float(avgvol.iloc[-1])
        relative_volume = float(volume.iloc[-1] / avgvol.iloc[-1])
        avg_dollar_volume_50 = float((close * volume).rolling(50).mean().iloc[-1])
        up_down_volume = None if down <= 0 else float(up / down)
    else:
        acc = dist = None
        avg_volume_50 = relative_volume = avg_dollar_volume_50 = up_down_volume = None
    reasons=[]
    if avg_dollar_volume_50 is None:
        reasons.append("volume evidence unavailable; liquidity and accumulation are insufficient evidence")
    elif avg_dollar_volume_50 < 20_000_000:
        reasons.append("average dollar volume below $20M")
    if price < sma200.iloc[-1]: reasons.append("below 200-day moving average")
    if price < sma50.iloc[-1]: reasons.append("below 50-day moving average")
    if (price/high52-1)*100 < -25: reasons.append("more than 25% below 52-week high")
    if (price/sma50.iloc[-1]-1)*100 > 20: reasons.append("more than 20% above 50-day moving average")
    gate = "DEPRIORITIZE" if reasons else "CHART_REVIEW"
    if not reasons:
        score=sum([price>=sma21.iloc[-1]>=sma50.iloc[-1]>=sma200.iloc[-1], (price/high52-1)*100>=-10, relative_volume is not None and relative_volume>=1, _tightness(df,10)<=12, acc is not None and dist is not None and acc>=dist])
        if score>=3: gate="CHART_REVIEW_PRIORITY"
    return Metrics(symbol, session_date, str(df.attrs.get("asset_class") or "EQUITY"), price, float(sma21.iloc[-1]), float(sma50.iloc[-1]), float(sma200.iloc[-1]), float(atr.iloc[-1]), float(atr.iloc[-1]/price*100), avg_volume_50, relative_volume, avg_dollar_volume_50, high52, float((price/high52-1)*100), float((price/sma50.iloc[-1]-1)*100), float((price/sma200.iloc[-1]-1)*100), _tightness(df,5), _tightness(df,10), _tightness(df,15), up_down_volume, acc, dist, gate, reasons)


def render_chart(symbol: str, df: pd.DataFrame, session_date: str, path: Path, weekly: bool=False, rs_line: pd.Series | None=None) -> None:
    df=validate_bars(symbol,df,session_date)
    volume_available = not df["Volume"].isna().any()
    if weekly:
        aggregation = {"Open":"first","High":"max","Low":"min","Close":"last"}
        if volume_available:
            aggregation["Volume"] = "sum"
        df=df.resample("W-FRI").agg(aggregation).dropna().tail(156)
        mav=(10,40); title=f"{symbol} — Weekly through {session_date}"
        if rs_line is not None and not rs_line.empty:
            rs_line=rs_line.resample("W-FRI").last().reindex(df.index).ffill()
    else:
        df=df.tail(252); mav=(21,50,200); title=f"{symbol} — Daily through {session_date}"
        if rs_line is not None and not rs_line.empty:
            rs_line=rs_line.reindex(df.index).ffill()
    path.parent.mkdir(parents=True,exist_ok=True)
    addplots=[]; kwargs={}
    if rs_line is not None and rs_line.notna().sum() >= 20:
        addplots=[mpf.make_addplot(rs_line, panel=2 if volume_available else 1, ylabel="RS vs S&P 500")]
        kwargs["panel_ratios"]=(5,1.4,1.2) if volume_available else (5,1.2)
    mpf.plot(df,type="candle",volume=volume_available,mav=mav,addplot=addplots,style="yahoo",title=title,figsize=(13,8.2),tight_layout=True,savefig=dict(fname=str(path),dpi=150,bbox_inches="tight"),**kwargs)
    plt.close("all")
    if not path.exists() or path.stat().st_size < 5000: raise ValidationError(f"{symbol}: chart render failed")


def _fmt_pct(value) -> str:
    return "N/A" if value is None else f"{float(value):.1f}%"


def _fmt_num(value, digits=2) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def build_pdf(session_date: str, records: list[dict], output: Path) -> None:
    styles=getSampleStyleSheet(); story=[Paragraph(f"Market Chart Packet — {session_date}",styles["Title"]),Spacer(1,10),Paragraph(f"Verified chart records: {len(records)}",styles["BodyText"]),Spacer(1,10)]
    for i,r in enumerate(records):
        m=r["metrics"]; e=r.get("fmp",{}); tech=r.get("technical_context",{}); earnings=e.get("earnings",{}); profile=e.get("profile",{}); qg=e.get("quarterly_growth",{}); price_evidence=e.get("historical_price_evidence",{})
        rs=tech.get("relative_strength",{}); vol=tech.get("volume",{}); base=tech.get("base_analysis",{})
        story.append(Paragraph(f"{m['ticker']} — {m['quantitative_gate']}",styles["Heading2"]))
        data=[["Price","21D","50D","200D","ATR%","Rel Vol","52W Dist"],[f"{m['current_price']:.2f}",f"{m['sma21']:.2f}",f"{m['sma50']:.2f}",f"{m['sma200']:.2f}",f"{m['atr_pct']:.1f}%",("N/A" if m.get("relative_volume") is None else f"{m['relative_volume']:.2f}x"),f"{m['pct_from_52w_high']:.1f}%"]]
        t=Table(data,repeatRows=1); t.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.lightgrey),("GRID",(0,0),(-1,-1),.25,colors.grey),("FONTSIZE",(0,0),(-1,-1),8)])); story.append(t); story.append(Spacer(1,5))
        earnings_label = earnings.get("earnings_date") or earnings.get("earnings_status") or "UNAVAILABLE"
        details=(
            f"FMP: {profile.get('sector') or 'Sector unavailable'} / {profile.get('industry') or 'Industry unavailable'} | "
            f"Earnings: {earnings_label} | Days: {earnings.get('days_to_earnings')} | "
            f"Q EPS growth: {_fmt_pct(qg.get('eps_growth_pct'))} | Q revenue growth: {_fmt_pct(qg.get('revenue_growth_pct'))} | "
            f"History: {price_evidence.get('provider') or 'FMP'} {price_evidence.get('endpoint') or 'stable historical EOD'} | "
            f"Adjustment: {price_evidence.get('adjustment') or 'UNAVAILABLE'} | Volume: {price_evidence.get('volume_status') or 'UNAVAILABLE'}"
        )
        story.append(Paragraph(details,styles["BodyText"])); story.append(Spacer(1,4))
        tech_details=(
            f"Technical: vs 21D {_fmt_pct(tech.get('distance_from_21d_pct'))} | vs 50D {_fmt_pct(tech.get('distance_from_50d_pct'))} | "
            f"10W {tech.get('ma_10w_trend')} ({_fmt_pct(tech.get('ma_10w_slope_4w_pct'))}) | 40W {tech.get('ma_40w_trend')} ({_fmt_pct(tech.get('ma_40w_slope_4w_pct'))}) | "
            f"RS {rs.get('trend_21d')} / New high: {rs.get('new_high_52w')} | Up/Down Vol 20D: {_fmt_num(vol.get('up_down_volume_ratio_20'))} | "
            f"A/D: {vol.get('accumulation_distribution_estimate')} | Base: {base.get('status')}"
        )
        story.append(Paragraph(tech_details,styles["BodyText"])); story.append(Spacer(1,6))
        story.append(Image(r["daily_chart"],width=520,height=315)); story.append(Spacer(1,6)); story.append(Image(r["weekly_chart"],width=520,height=315))
        if i < len(records)-1: story.append(PageBreak())
    SimpleDocTemplate(str(output),pagesize=letter,rightMargin=26,leftMargin=26,topMargin=24,bottomMargin=24).build(story)


def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()


def serialize_price_history(df: pd.DataFrame, limit: int = 500) -> list[dict]:
    """Keep enough verified daily evidence for position-aware trailing rules."""
    rows = []
    for index, row in df.tail(limit).iterrows():
        rows.append(
            {
                "date": index.date().isoformat(),
                "open": round(float(row["Open"]), 6),
                "high": round(float(row["High"]), 6),
                "low": round(float(row["Low"]), 6),
                "close": round(float(row["Close"]), 6),
                "volume": None if pd.isna(row["Volume"]) else round(float(row["Volume"]), 6),
            }
        )
    return rows


def build_packet(symbols: Iterable[str], session_date: str, output_dir: Path, provenance: dict | None=None) -> dict:
    symbols=normalize_symbols(symbols); output_dir.mkdir(parents=True,exist_ok=True); charts=output_dir/"charts"
    policy_payload = json.loads((Path(__file__).resolve().parents[1] / "config" / "trading_policy.json").read_text(encoding="utf-8"))
    pattern_policy = policy_payload.get("pattern_engine") or {}
    bars, fetch_errors=fetch_bars(symbols,session_date); records=[]; errors=dict(fetch_errors); enrichment_errors={}
    start=(pd.Timestamp(session_date)-pd.Timedelta(days=1100)).date().isoformat()
    try:
        benchmark=historical_eod("^GSPC",start,session_date)
        if len(benchmark) < 200:
            raise ValidationError(f"^GSPC: {len(benchmark)} bars available; 200 required")
        expected_session = pd.Timestamp(session_date).normalize()
        if benchmark.index[-1] != expected_session:
            raise ValidationError(
                f"^GSPC: latest bar {benchmark.index[-1].date()} != requested session {expected_session.date()}"
            )
        if benchmark["Close"].isna().any() or benchmark["Close"].le(0).any():
            raise ValidationError("^GSPC: invalid benchmark close history")
    except Exception as exc:
        benchmark=pd.DataFrame(); enrichment_errors["^GSPC"]=str(exc)
    for symbol in symbols:
        try:
            if symbol not in bars: raise ValidationError(f"{symbol}: no bars returned")
            m=calculate_metrics(symbol,bars[symbol],session_date)
            fmp_data={}; rs=pd.Series(dtype=float)
            try:
                fmp_data,rs=enrich_symbol(symbol,session_date,bars[symbol],benchmark)
            except Exception as exc:
                enrichment_errors[symbol]=str(exc)
                fmp_data={"provider":"FMP","status":"ERROR","error":str(exc),"historical_price_evidence":price_history_metadata(bars[symbol])}
            technical_context=calculate_technical_context(bars[symbol],rs,pattern_policy)
            daily=charts/f"{symbol}_daily.png"; weekly=charts/f"{symbol}_weekly.png"; pattern_chart=charts/f"{symbol}_pattern.png"
            render_chart(symbol,bars[symbol],session_date,daily,False,rs); render_chart(symbol,bars[symbol],session_date,weekly,True,rs)
            render_pattern_chart(symbol,bars[symbol],session_date,technical_context["base_analysis"],pattern_chart)
            records.append({"metrics":asdict(m),"technical_context":technical_context,"fmp":fmp_data,"price_data":price_history_metadata(bars[symbol]),"sources":(provenance or {}).get(symbol,[]),"daily_chart":str(daily),"weekly_chart":str(weekly),"pattern_chart":str(pattern_chart),"latest_bar_date":bars[symbol].index[-1].date().isoformat(),"price_history":serialize_price_history(bars[symbol])})
        except Exception as exc: errors[symbol]=str(exc)
    status="COMPLETE" if not errors and not enrichment_errors else "COMPLETE_WITH_WARNINGS"
    payload={"session_date":session_date,"requested_tickers":symbols,"verified_count":len(records),"error_count":len(errors),"errors":errors,"enrichment_errors":enrichment_errors,"records":records,"status":status,"chart_data_source":"FMP","chart_data_endpoint":"stable/historical-price-eod/full","chart_data_policy":{"bounded_history":True,"exact_session_required":True,"live_quote_substitution":False,"controlled_concurrency":history_concurrency()},"pattern_algorithm_version":PATTERN_ALGORITHM_VERSION,"pattern_policy_version":pattern_policy.get("policy_version"),"enrichment_source":"FMP","benchmark":"^GSPC"}
    json_path=output_dir/f"Market_Chart_Data_{session_date}.json"; json_path.write_text(json.dumps(payload,indent=2),encoding="utf-8")
    pdf_path=output_dir/f"Market_Chart_Packet_{session_date}.pdf"; build_pdf(session_date,records,pdf_path)
    payload["artifacts"]={"json":str(json_path),"pdf":str(pdf_path),"pdf_sha256":sha256(pdf_path)}
    json_path.write_text(json.dumps(payload,indent=2),encoding="utf-8")
    return payload
