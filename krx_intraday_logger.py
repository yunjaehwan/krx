# -*- coding: utf-8 -*-
"""
KRX 장중 가격 스냅샷 기록기 (미래의 분봉 백테스트용 데이터 수집)
====================================================================
과거 분봉 데이터는 무료로 구할 방법이 없어서, "9시 급등 -> 10시
눌림 -> 11시 재상승" 같은 장중 패턴은 지금 당장 백테스트가
불가능합니다. 대신 지금부터 5분마다 가격을 계속 기록해서 쌓아두면,
몇 주~몇 달 뒤엔 우리만의 장중 데이터가 생겨서 그때 검증할 수
있습니다.

krx_realtime_alert.py 와 같은 GitHub Actions 스케줄(장중 5분마다)에
얹어서 돌아가고, 그날그날 가격을 CSV에 계속 쌓습니다.

사전 준비
---------
    pip install pandas requests finance-datareader

저장 위치
---------
    ./krx_intraday_logs/log_YYYYMMDD.csv
    (날짜별로 파일이 쌓입니다. 컬럼: 시각,종목코드,종목명,현재가,누적거래량)
"""

import sys
import os
import csv
import datetime

import pandas as pd

try:
    import FinanceDataReader as fdr
except ImportError:
    print("FinanceDataReader가 설치되어 있지 않습니다. pip install finance-datareader")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("requests가 설치되어 있지 않습니다. pip install requests")
    sys.exit(1)


PREFILTER_TOP_N = 300           # 상위 몇 종목을 계속 기록할지 (너무 많으면 시간이 오래 걸림)
MIN_MARKET_CAP = 100_000_000_000  # 1,000억원

LOG_DIR = "./krx_intraday_logs"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = datetime.timezone(datetime.timedelta(hours=9))


def is_market_hours(now=None):
    now = now or datetime.datetime.now(KST)
    if now.weekday() >= 5:
        return False, now
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t, now


def build_or_load_candidate_list(date_str):
    """오늘 하루 동안 기록할 종목 목록. 자가치유 캐시 (0개면 다시 만듦)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"candidates_{date_str}.csv")
    if os.path.exists(path):
        cached = pd.read_csv(path, dtype={"종목코드": str})
        if len(cached) > 0:
            return list(zip(cached["종목코드"], cached["종목명"]))
        print("캐시된 후보가 0개라 다시 만듭니다...")

    print("전 종목 목록 조회 중...")
    df = fdr.StockListing("KRX")
    rename_map = {}
    for col in df.columns:
        lc = str(col).lower()
        if lc == "code":
            rename_map[col] = "종목코드"
        elif lc == "name":
            rename_map[col] = "종목명"
        elif lc == "marcap":
            rename_map[col] = "시가총액"
    df = df.rename(columns=rename_map)
    keep = [c for c in ["종목코드", "종목명", "시가총액"] if c in df.columns]
    df = df[keep].dropna(subset=["종목코드", "시가총액"])

    if "시가총액" in df.columns and df["시가총액"].notna().sum() > 0:
        df = df[df["시가총액"] >= MIN_MARKET_CAP]

    df = df.sort_values("시가총액", ascending=False).head(PREFILTER_TOP_N)
    if len(df) == 0:
        print("경고: 후보가 0개입니다. 캐시에 저장하지 않고 다음 실행에서 재시도합니다.")
        return []

    df[["종목코드", "종목명"]].to_csv(path, index=False, encoding="utf-8-sig")
    return list(zip(df["종목코드"], df["종목명"]))


def fetch_realtime_quote(ticker):
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{ticker}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=5)
        data = resp.json()
        d = data["datas"][0]
        price = float(str(d.get("closePrice", "")).replace(",", ""))

        volume = None
        for key in ("accTradeVolume", "accumulatedTradingVolume", "tradeVolume", "volume"):
            if key in d and d[key] not in (None, ""):
                try:
                    volume = float(str(d[key]).replace(",", ""))
                    break
                except (ValueError, TypeError):
                    continue
        return price, volume
    except Exception:
        return None, None


def run_snapshot():
    ok, now = is_market_hours()
    if not ok:
        print(f"장 시간이 아닙니다 ({now.strftime('%Y-%m-%d %H:%M')} KST). 종료합니다.")
        return

    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H:%M")

    candidates = build_or_load_candidate_list(date_str)
    if not candidates:
        print("기록할 후보 종목이 없습니다.")
        return

    log_path = os.path.join(LOG_DIR, f"log_{date_str}.csv")
    is_new_file = not os.path.exists(log_path)

    print(f"[{time_str}] {len(candidates)}개 종목 스냅샷 기록 중...")

    rows_written = 0
    with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["시각", "종목코드", "종목명", "현재가", "누적거래량"])
        for ticker, name in candidates:
            price, volume = fetch_realtime_quote(ticker)
            if price is None:
                continue
            writer.writerow([time_str, ticker, name, int(price), int(volume) if volume else ""])
            rows_written += 1

    print(f"기록 완료: {rows_written}개 종목, {log_path}")


if __name__ == "__main__":
    run_snapshot()
