# -*- coding: utf-8 -*-
"""
KRX 장중 거래량 급증 알림 (텔레그램 전송)
==========================================
장중(평일 9:00~15:30)에 전일 거래량 대비 오늘 누적 거래량이
비정상적으로 빠르게 쌓이는 + 주가도 오르고 있는 종목을 찾아서
텔레그램으로 알림을 보냅니다.

GitHub Actions 같은 무료 스케줄러에서 5분마다 이 스크립트를 실행하는
방식으로 쓰도록 설계했습니다 (내 컴퓨터가 꺼져있어도 동작).

동작 방식
---------
1. (하루 중 처음 실행될 때만) 전일 종가/거래량/시가총액 스냅샷을 만들어
   krx_alerts/baseline_YYYYMMDD.csv 로 저장해둡니다. (같은 날 재실행되면
   이미 있으면 다시 안 만들고 재사용 - 속도 위해)
2. 매 실행마다, 후보 종목들의 실시간 현재가/거래량을 조회합니다.
3. "오늘 지금까지 쌓였어야 할 예상 거래량"(전일 거래량 * 하루 경과 비율)
   대비 실제 거래량이 기준치 이상으로 많고, 주가도 오르고 있으면
   "급증 신호"로 판단합니다.
4. 오늘 이미 알림을 보낸 종목은 중복으로 또 보내지 않습니다.
   (krx_alerts/alerted_YYYYMMDD.json 에 기록)

사전 준비
---------
    pip install pandas requests finance-datareader

환경변수 (GitHub Actions Secrets 로 등록하거나, 로컬 테스트시 직접 설정)
---------
    TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
    TELEGRAM_CHAT_ID   : 내 채팅 ID

디버그 (실시간 API 응답 구조가 궁금하거나 문제가 생겼을 때)
---------
    python krx_realtime_alert.py --debug 005930
    -> 해당 종목의 실시간 API 원본 응답을 그대로 출력합니다.
       이 출력을 저한테 보여주시면 필드명 문제를 바로 고칠 수 있어요.

주의
----
- 네이버 금융의 공개 실시간 시세 API를 사용합니다. 이 구조가 바뀌면
  스크립트가 깨질 수 있어요. (--debug 로 원본 응답 확인 가능)
- 매수 추천이 아니라 조건에 맞는 종목을 걸러 알려주는 필터입니다.
- 공휴일 판단은 하지 않습니다 (평일이면 그냥 실행됨). 공휴일엔 어차피
  거래량이 없어서 신호가 안 잡힐 뿐이라 큰 문제는 없습니다.
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

PREFILTER_TOP_N = 200          # 후보로 추릴 종목 수 (거래대금 상위)
MIN_MARKET_CAP = 50_000_000_000  # 최소 시가총액 (500억원)
SURGE_RATIO_THRESHOLD = 2.5    # "예상 거래량" 대비 몇 배 이상이면 급증으로 볼지
MIN_ABS_VOLUME = 50_000        # 최소 절대 거래량 (너무 작은 종목 노이즈 방지)
MIN_PRICE_CHANGE_PCT = 1.0     # 최소 주가 상승률 (%) - 이것도 만족해야 신호
MAX_ALERTS_PER_RUN = 15        # 한 번 실행에 너무 많은 신호가 뜨면 요약 메시지로 전환
REQUEST_DELAY = 0.1            # 종목별 조회 사이 대기(초)

ALERT_DIR = "./krx_alerts"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = datetime.timezone(datetime.timedelta(hours=9))


# ============================================================
# 장중 시간 확인
# ============================================================

def is_market_hours(now=None):
    now = now or datetime.datetime.now(KST)
    if now.weekday() >= 5:  # 토(5), 일(6)
        return False, now
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t, now


def day_fraction_elapsed(now):
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    total = (close_t - open_t).total_seconds()
    elapsed = (now - open_t).total_seconds()
    frac = elapsed / total
    return max(0.03, min(frac, 1.0))


# ============================================================
# 전일 기준 스냅샷 (하루 1번만 생성, 이후 재사용)
# ============================================================

def build_or_load_baseline(date_str):
    os.makedirs(ALERT_DIR, exist_ok=True)
    path = os.path.join(ALERT_DIR, f"baseline_{date_str}.csv")
    if os.path.exists(path):
        cached = pd.read_csv(path, dtype={"종목코드": str}).set_index("종목코드")
        if len(cached) > 0:
            return cached
        print("캐시된 후보 목록이 0개라 무효 처리하고 다시 만듭니다...")

    print("전일 기준 스냅샷 생성 중...")
    df = fdr.StockListing("KRX")
    rename_map = {}
    for col in df.columns:
        lc = str(col).lower()
        if lc == "code":
            rename_map[col] = "종목코드"
        elif lc == "name":
            rename_map[col] = "종목명"
        elif lc == "close":
            rename_map[col] = "전일종가"
        elif lc == "volume":
            rename_map[col] = "전일거래량"
        elif lc == "marcap":
            rename_map[col] = "시가총액"
    df = df.rename(columns=rename_map)
    keep = [c for c in ["종목코드", "종목명", "전일종가", "전일거래량", "시가총액"] if c in df.columns]
    df = df[keep].dropna(subset=["종목코드", "전일종가"])
    print(f"  1) 전체 상장 종목: {len(df)}개")

    df["거래대금"] = df["전일거래량"] * df["전일종가"]

    if "시가총액" in df.columns and df["시가총액"].notna().sum() > 0:
        df = df[df["시가총액"] >= MIN_MARKET_CAP]
        print(f"  2) 시가총액 {MIN_MARKET_CAP/1e8:.0f}억 이상 필터 후: {len(df)}개")
    else:
        print("  2) 시가총액 데이터를 못 가져와서 이 필터는 건너뜁니다 (거래대금만으로 순위 매김)")

    df = df.sort_values("거래대금", ascending=False).head(PREFILTER_TOP_N)
    print(f"  3) 거래대금 상위 {PREFILTER_TOP_N}개로 최종 후보 확정: {len(df)}개")

    if len(df) == 0:
        print("경고: 최종 후보가 0개입니다. 원본 데이터 소스에 문제가 있을 수 있습니다. 캐시에 저장하지 않고 다음 실행에서 재시도합니다.")
        return df.set_index("종목코드") if "종목코드" in df.columns else pd.DataFrame()

    df.set_index("종목코드").to_csv(path, encoding="utf-8-sig")
    return df.set_index("종목코드")


# ============================================================
# 실시간 시세 조회
# ============================================================

def fetch_realtime_quote(ticker):
    """네이버 금융 실시간 시세 API. (price, volume) 튜플 반환, 실패시 (None, None)."""
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
# 알림 상태 (중복 전송 방지)
# ============================================================

def load_alerted(date_str):
    path = os.path.join(ALERT_DIR, f"alerted_{date_str}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_alerted(date_str, alerted_set):
    path = os.path.join(ALERT_DIR, f"alerted_{date_str}.json")
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
        print("(로컬 테스트라면 환경변수를 설정하거나, GitHub Actions라면 Secrets를 확인하세요)")
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
    ok, now = is_market_hours()
    if not ok:
        print(f"장 시간이 아닙니다 ({now.strftime('%Y-%m-%d %H:%M')} KST). 종료합니다.")
        return

    date_str = now.strftime("%Y%m%d")
    baseline = build_or_load_baseline(date_str)
    alerted = load_alerted(date_str)
    frac = day_fraction_elapsed(now)

    print(f"[{now.strftime('%H:%M')}] {len(baseline)}개 후보 종목 실시간 확인 중... (하루 경과율 {frac:.0%})")

    new_signals = []
    for ticker, row in baseline.iterrows():
        if ticker in alerted:
            continue
        price, volume = fetch_realtime_quote(ticker)
        if price is None or volume is None:
            continue

        prev_close = row["전일종가"]
        prev_volume = row["전일거래량"]
        if prev_close <= 0 or prev_volume <= 0:
            continue

        expected_volume = prev_volume * frac
        ratio = volume / max(expected_volume, 1)
        price_change_pct = (price - prev_close) / prev_close * 100

        if (ratio >= SURGE_RATIO_THRESHOLD
                and volume >= MIN_ABS_VOLUME
                and price_change_pct >= MIN_PRICE_CHANGE_PCT):
            new_signals.append({
                "종목코드": ticker,
                "종목명": row["종목명"],
                "현재가": int(price),
                "등락률(%)": round(price_change_pct, 2),
                "거래량배수": round(ratio, 1),
            })
            alerted.add(ticker)

    save_alerted(date_str, alerted)

    if not new_signals:
        print("새로운 급증 신호 없음.")
        return

    print(f"새 신호 {len(new_signals)}건 발견, 텔레그램 전송 중...")

    if len(new_signals) > MAX_ALERTS_PER_RUN:
        lines = [f"[KRX 급등 알림] 한 번에 {len(new_signals)}개 종목 포착 (장 초반 변동성일 수 있어요)"]
        for s in new_signals[:MAX_ALERTS_PER_RUN]:
            lines.append(f"- {s['종목명']} {s['현재가']:,}원 (+{s['등락률(%)']}%, 거래량 {s['거래량배수']}배)")
        send_telegram("\n".join(lines))
    else:
        for s in new_signals:
            msg = (
                f"[KRX 거래량 급증]\n"
                f"{s['종목명']} ({s['종목코드']})\n"
                f"현재가: {s['현재가']:,}원 (+{s['등락률(%)']}%)\n"
                f"평소 대비 거래량: {s['거래량배수']}배"
            )
            send_telegram(msg)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--debug":
        debug_quote(sys.argv[2])
        return
    run_scan()


if __name__ == "__main__":
    main()
