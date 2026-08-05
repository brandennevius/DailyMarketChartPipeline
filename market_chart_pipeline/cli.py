from __future__ import annotations
import argparse
from pathlib import Path
from .core import build_packet


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--session-date",required=True)
    p.add_argument("--tickers",required=True,help="Comma-separated current-session manifest tickers")
    p.add_argument("--output-dir",default="output")
    p.add_argument("--feed",default="iex",choices=["iex","sip"])
    a=p.parse_args()
    tickers=[x.strip().upper() for x in a.tickers.split(",") if x.strip()]
    result=build_packet(tickers,a.session_date,Path(a.output_dir),a.feed)
    print(f"status={result['status']} verified={result['verified_count']} errors={result['error_count']}")

if __name__=="__main__": main()
