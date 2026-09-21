# -*- coding: utf-8 -*-
"""
KRX 애프터마켓 실시간 급등 알림 (텔레그램 전송)
==================================================
애프터마켓(KRX 애프터마켓 + 넥스트레이드 애프터마켓, 15:30~20:00)에
정규장 종가 대비 급등한 종목을 감시해서 텔레그램으로 알려줍니다.

krx_night_scanner.py 가 저녁에 한 번 스냅샷을 찍는 방식이라면, 이건
krx_realtime_alert.py 처럼 그 시간대 내내 5분마다 계속 감시하다가
조건에 맞으면 바로바로 알림을 보내는 버전입니다.

GitHub Actions 같은 무료 스케줄러에서 15:30~20:00 사이 5분마다
이 스크립트를 실행하도록 설계했습니다 (컴퓨터가 꺼져있어도 동작).

기준
----
    "오늘 정규장 종가" 대비 실시간(애프터마켓) 가격이 AFTERHOURS_SURGE_PCT
    이상 올랐으면 신호로 봅니다. (넥스트레이드/KRX 애프터마켓 구분 없이,
    네이버 금융 실시간 시세에 잡히는 가격을 그대로 사용합니다)

사전 준비
---------
    pip install pandas requests finance-datareader

환경변수
---------
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

디버그
------
    python krx_afterhours_alert.py --debug 005930
"""

import sys
import os
import json
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


# ============================================================
# 설정값
# ============================================================

PREFILTER_TOP_N = 300
MIN_MARKET_CAP = 50_000_000_000  # 500억원
AFTERHOURS_SURGE_PCT = 3.0       # 정규장 종가 대비 몇 % 이상 오르면 신호로 볼지
MIN_ABS_VOLUME = 10_000          # 최소 절대 거래량 (너무 적은 거래량 노이즈 방지)
MAX_ALERTS_PER_RUN = 15
REQUEST_DELAY = 0.1

ALERT_DIR = "./krx_alerts"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = datetime.timezone(datetime.timedelta(hours=9))


# ============================================================
# 애프터마켓 시간 확인
# ============================================================

def is_afterhours(now=None):
    now = now or datetime.datetime.now(KST)
    if now.weekday() >= 5:
        return False, now
    start_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    end_t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    return start_t <= now <= end_t, now


# ============================================================
# 오늘 정규장 종가/거래량 스냅샷 (하루 1번, 자가치유 캐시)
# ============================================================

def _coerce_numeric_baseline(df):
    for col in ["정규장종가", "정규장거래량", "시가총액"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=[c for c in ["정규장종가", "정규장거래량"] if c in df.columns])


def build_or_load_today_snapshot(date_str):
    os.makedirs(ALERT_DIR, exist_ok=True)
    path = os.path.join(ALERT_DIR, f"afterhours_baseline_{date_str}.csv")
    if os.path.exists(path):
        cached = pd.read_csv(path, dtype={"종목코드": str}).set_index("종목코드")
        cached = _coerce_numeric_baseline(cached)
        if len(cached) > 0:
            return cached
        print("캐시된 후보 목록이 0개라 무효 처리하고 다시 만듭니다...")

    print("오늘 정규장 종가 스냅샷 생성 중...")
    df = fdr.StockListing("KRX")
    rename_map = {}
    for col in df.columns:
        lc = str(col).lower()
        if lc == "code":
            rename_map[col] = "종목코드"
        elif lc == "name":
            rename_map[col] = "종목명"
        elif lc == "close":
            rename_map[col] = "정규장종가"
        elif lc == "volume":
            rename_map[col] = "정규장거래량"
        elif lc == "marcap":
            rename_map[col] = "시가총액"
    df = df.rename(columns=rename_map)
    keep = [c for c in ["종목코드", "종목명", "정규장종가", "정규장거래량", "시가총액"] if c in df.columns]
    df = df[keep].dropna(subset=["종목코드", "정규장종가"])
    df = _coerce_numeric_baseline(df)
    print(f"  1) 전체 상장 종목: {len(df)}개")

    df["거래대금"] = df["정규장거래량"] * df["정규장종가"]

    if "시가총액" in df.columns and df["시가총액"].notna().sum() > 0:
        df = df[df["시가총액"] >= MIN_MARKET_CAP]
        print(f"  2) 시가총액 {MIN_MARKET_CAP/1e8:.0f}억 이상 필터 후: {len(df)}개")
    else:
        print("  2) 시가총액 데이터를 못 가져와서 이 필터는 건너뜁니다")

    df = df.sort_values("거래대금", ascending=False).head(PREFILTER_TOP_N)
    print(f"  3) 거래대금 상위 {PREFILTER_TOP_N}개로 최종 후보 확정: {len(df)}개")

    if len(df) == 0:
        print("경고: 최종 후보가 0개입니다. 캐시에 저장하지 않고 다음 실행에서 재시도합니다.")
        return df.set_index("종목코드") if "종목코드" in df.columns else pd.DataFrame()

    df.set_index("종목코드").to_csv(path, encoding="utf-8-sig")
    return df.set_index("종목코드")


# ============================================================
# 실시간 시세 조회 (KRX 애프터마켓 + 넥스트레이드 애프터마켓 포함)
# ============================================================

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


def debug_quote(ticker):
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{ticker}"
    resp = requests.get(url, headers=HEADERS, timeout=5)
    print(f"URL: {url}")
    print(f"상태코드: {resp.status_code}")
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))


# ============================================================
# 알림 상태 (중복 전송 방지, 정규장 알림과 별도 파일 사용)
# ============================================================

def load_alerted(date_str):
    path = os.path.join(ALERT_DIR, f"afterhours_alerted_{date_str}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_alerted(date_str, alerted_set):
    path = os.path.join(ALERT_DIR, f"afterhours_alerted_{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(alerted_set), f, ensure_ascii=False)


# ============================================================
# 텔레그램 전송
# ============================================================

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없어 전송을 건너뜁니다.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        res = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
        if res.status_code != 200:
            print(f"텔레그램 전송 실패: {res.status_code} {res.text}")
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")


# ============================================================
# 메인 스캔
# ============================================================

def run_scan():
    ok, now = is_afterhours()
    if not ok:
        print(f"애프터마켓 시간이 아닙니다 ({now.strftime('%Y-%m-%d %H:%M')} KST). 종료합니다.")
        return

    date_str = now.strftime("%Y%m%d")
    baseline = build_or_load_today_snapshot(date_str)
    if len(baseline) == 0:
        print("후보 종목이 0개라 이번 실행은 건너뜁니다.")
        return

    alerted = load_alerted(date_str)

    print(f"[{now.strftime('%H:%M')}] {len(baseline)}개 후보 종목 애프터마켓 시세 확인 중...")

    new_signals = []
    for ticker, row in baseline.iterrows():
        if ticker in alerted:
            continue
        price, volume = fetch_realtime_quote(ticker)
        if price is None:
            continue

        reg_close = row["정규장종가"]
        if reg_close <= 0:
            continue

        pct = (price - reg_close) / reg_close * 100
        vol_ok = (volume is None) or (volume >= MIN_ABS_VOLUME)

        if pct >= AFTERHOURS_SURGE_PCT and vol_ok:
            new_signals.append({
                "종목코드": ticker,
                "종목명": row["종목명"],
                "정규장종가": int(reg_close),
                "현재가": int(price),
                "등락률(%)": round(pct, 2),
            })
            alerted.add(ticker)

    save_alerted(date_str, alerted)

    if not new_signals:
        print("새로운 애프터마켓 급등 신호 없음.")
        return

    print(f"새 신호 {len(new_signals)}건 발견, 텔레그램 전송 중...")

    if len(new_signals) > MAX_ALERTS_PER_RUN:
        lines = [f"[KRX 애프터마켓 급등] 한 번에 {len(new_signals)}개 종목 포착"]
        for s in new_signals[:MAX_ALERTS_PER_RUN]:
            lines.append(f"- {s['종목명']} {s['현재가']:,}원 (+{s['등락률(%)']}%)")
        send_telegram("\n".join(lines))
    else:
        for s in new_signals:
            msg = (
                f"[KRX 애프터마켓 급등]\n"
                f"{s['종목명']} ({s['종목코드']})\n"
                f"정규장 종가: {s['정규장종가']:,}원\n"
                f"현재가: {s['현재가']:,}원 (+{s['등락률(%)']}%)"
            )
            send_telegram(msg)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--debug":
        debug_quote(sys.argv[2])
        return
    run_scan()


if __name__ == "__main__":
    main()
