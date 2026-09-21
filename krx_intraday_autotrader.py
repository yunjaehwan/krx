# -*- coding: utf-8 -*-
"""
KRX 장중 실시간 추격 자동매매 봇 (모의투자, 실험적)
========================================================
krx_autotrader.py 는 "그날 마감 기준으로 판단, 다음날 정리"하는
검증된(백테스트 완료) 방식입니다. 이 스크립트는 그것과 별개로,
"장중에 튀는 순간 바로 사서, 고점 대비 3% 빠지면 바로 파는" 방식
입니다.

※ 중요: 이 방식은 분 단위 데이터가 없어서 과거로 백테스트를
할 수 없었습니다. 그래서 검증된 전략이 아니라 "실험"입니다.
모의투자로 실시간으로 돌려보면서 실제로 어떤지 확인하는 용도입니다.
절대 이 결과만 보고 바로 실전(진짜 돈)으로 넘어가지 마세요.

전략
----
    - 5분마다 후보 종목 감시
    - 전일 대비 거래량 3배 이상 + 오늘 상승률 5~25% 종목 발견 시 즉시 매수
      (동시에 최대 TOP_K개까지)
    - 매수 이후 고점을 계속 추적, 고점 대비 3% 하락하면 즉시 매도
    - 15:20 이후에도 안 팔린 종목은 장마감 전 강제 매도 (오버나이트 안 함)

krx_autotrader.py 와 완전히 독립적으로 돌아갑니다 (상태 파일도 별도).

사전 준비
---------
    pip install requests pandas finance-datareader

환경변수 (GitHub Secrets, krx_autotrader.py 와 동일한 것 재사용)
---------
    KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import sys
import os
import json
import datetime

import requests
import pandas as pd

try:
    import FinanceDataReader as fdr
except ImportError:
    print("FinanceDataReader가 설치되어 있지 않습니다. pip install finance-datareader")
    sys.exit(1)


IS_MOCK = True  # 절대 True 로만 두세요.

SURGE_RATIO = 3.0
MIN_CHANGE_PCT = 5.0
MAX_CHANGE_PCT = 12.0
TRAILING_STOP_PCT = 3.0   # 고점 대비 이만큼(%) 빠지면 매도
TOP_K = 2

MIN_MARKET_CAP = 100_000_000_000
PREFILTER_TOP_N = 300

FORCE_CLOSE_TIME = "15:20"  # 이 시각부터는 안 팔린 포지션 강제 정리, 신규 진입도 안 함

STATE_DIR = "./krx_intraday_state"
HEADERS_NAVER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = datetime.timezone(datetime.timedelta(hours=9))

KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_MOCK else "https://openapi.koreainvestment.com:9443"
TR_BUY = "VTTC0802U" if IS_MOCK else "TTTC0802U"
TR_SELL = "VTTC0801U" if IS_MOCK else "TTTC0801U"


# ============================================================
# KIS API (krx_autotrader.py 와 동일)
# ============================================================

def get_kis_credentials():
    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    account_no = os.environ.get("KIS_ACCOUNT_NO")
    if not app_key or not app_secret or not account_no:
        print("KIS 환경변수가 없습니다.")
        sys.exit(1)
    cano, prdt_cd = account_no.split("-")
    return app_key, app_secret, cano, prdt_cd


def get_access_token(app_key, app_secret):
    os.makedirs(STATE_DIR, exist_ok=True)
    token_path = os.path.join(STATE_DIR, "kis_token.json")

    if os.path.exists(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            expire_at = datetime.datetime.fromisoformat(cached["expire_at"])
            if datetime.datetime.now(KST) < expire_at:
                return cached["access_token"]
        except Exception:
            pass

    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    res = requests.post(url, json=body, timeout=10)
    if res.status_code != 200:
        print(f"토큰 발급 실패! 상태코드: {res.status_code}")
        print(f"응답 내용: {res.text}")
    res.raise_for_status()
    data = res.json()
    token = data["access_token"]
    expire_at = datetime.datetime.now(KST) + datetime.timedelta(hours=20)
    with open(token_path, "w", encoding="utf-8") as f:
        json.dump({"access_token": token, "expire_at": expire_at.isoformat()}, f)
    return token


def get_hashkey(app_key, app_secret, body):
    url = f"{KIS_BASE_URL}/uapi/hashkey"
    headers = {"content-type": "application/json", "appkey": app_key, "appsecret": app_secret}
    res = requests.post(url, headers=headers, json=body, timeout=10)
    res.raise_for_status()
    return res.json()["HASH"]


def kis_place_order(ticker, qty, side, app_key, app_secret, token, cano, prdt_cd):
    tr_id = TR_BUY if side == "buy" else TR_SELL
    body = {
        "CANO": cano, "ACNT_PRDT_CD": prdt_cd,
        "PDNO": ticker, "ORD_DVSN": "01",
        "ORD_QTY": str(int(qty)), "ORD_UNPR": "0",
    }
    hashkey = get_hashkey(app_key, app_secret, body)
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": tr_id, "custtype": "P", "hashkey": hashkey,
    }
    res = requests.post(url=f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
                         headers=headers, json=body, timeout=10)
    return res.json()


def kis_get_balance(app_key, app_secret, token, cano, prdt_cd):
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": "VTTC8434R" if IS_MOCK else "TTTC8434R",
    }
    params = {
        "CANO": cano, "ACNT_PRDT_CD": prdt_cd,
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
        "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }
    res = requests.get(url, headers=headers, params=params, timeout=10)
    data = res.json()
    cash = 0
    try:
        cash = float(data["output2"][0]["dnca_tot_amt"])
    except Exception:
        pass
    return cash


# ============================================================
# 시간 유틸
# ============================================================

def now_kst():
    return datetime.datetime.now(KST)


def is_market_hours(now):
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def is_force_close_time(now):
    return now.strftime("%H:%M") >= FORCE_CLOSE_TIME


# ============================================================
# 후보 종목 + 실시간 시세 (krx_realtime_alert.py 와 동일 패턴)
# ============================================================

def _coerce_numeric_baseline(df):
    for col in ["전일종가", "전일거래량", "시가총액"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=[c for c in ["전일종가", "전일거래량"] if c in df.columns])


def build_or_load_baseline(date_str):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"baseline_{date_str}.csv")
    if os.path.exists(path):
        cached = pd.read_csv(path, dtype={"종목코드": str}).set_index("종목코드")
        cached = _coerce_numeric_baseline(cached)
        if len(cached) > 0:
            return cached

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
    df = _coerce_numeric_baseline(df)

    if "시가총액" in df.columns and df["시가총액"].notna().sum() > 0:
        df = df[df["시가총액"] >= MIN_MARKET_CAP]

    df["거래대금"] = df["전일거래량"] * df["전일종가"]
    df = df.sort_values("거래대금", ascending=False).head(PREFILTER_TOP_N)

    if len(df) == 0:
        return pd.DataFrame()

    df.set_index("종목코드").to_csv(path, encoding="utf-8-sig")
    return df.set_index("종목코드")


def fetch_realtime_quote(ticker):
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{ticker}"
    try:
        resp = requests.get(url, headers=HEADERS_NAVER, timeout=5)
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


# ============================================================
# 상태 관리 / 텔레그램
# ============================================================

def load_state():
    path = os.path.join(STATE_DIR, "positions.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": []}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "positions.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")


# ============================================================
# 메인 로직
# ============================================================

def check_positions(state, app_key, app_secret, token, cano, prdt_cd, force_close):
    remaining = []
    for pos in state["positions"]:
        price, _ = fetch_realtime_quote(pos["ticker"])
        if price is None:
            remaining.append(pos)
            continue

        pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
        change_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        drop_from_peak = (price - pos["peak_price"]) / pos["peak_price"] * 100

        should_sell = False
        reason = ""
        if drop_from_peak <= -TRAILING_STOP_PCT:
            should_sell, reason = True, "추적매도"
        elif force_close:
            should_sell, reason = True, "장마감 강제매도"

        if should_sell:
            result = kis_place_order(pos["ticker"], pos["qty"], "sell", app_key, app_secret, token, cano, prdt_cd)
            msg = (f"[장중봇 매도-{reason}]\n{pos['name']} ({pos['ticker']})\n"
                   f"매수가: {pos['entry_price']:,.0f}원 -> 현재가: {price:,.0f}원 ({change_pct:+.2f}%)\n"
                   f"주문결과: {result.get('msg1', result)}")
            print(msg)
            send_telegram(msg)
        else:
            remaining.append(pos)

    state["positions"] = remaining
    return state


def try_enter_new_positions(state, app_key, app_secret, token, cano, prdt_cd, now):
    free_slots = TOP_K - len(state["positions"])
    if free_slots <= 0:
        return state

    date_str = now.strftime("%Y%m%d")
    baseline = build_or_load_baseline(date_str)
    if len(baseline) == 0:
        return state

    held_codes = {p["ticker"] for p in state["positions"]}
    signals = []
    for ticker, row in baseline.iterrows():
        if ticker in held_codes:
            continue
        price, volume = fetch_realtime_quote(ticker)
        if price is None or volume is None:
            continue
        prev_close = row["전일종가"]
        prev_volume = row["전일거래량"]
        if prev_close <= 0 or prev_volume <= 0:
            continue
        chg_pct = (price - prev_close) / prev_close * 100
        vol_ratio = volume / prev_volume
        if vol_ratio >= SURGE_RATIO and MIN_CHANGE_PCT <= chg_pct <= MAX_CHANGE_PCT:
            signals.append({"ticker": ticker, "name": row["종목명"], "price": price, "chg_pct": chg_pct})

    if not signals:
        return state

    signals.sort(key=lambda x: x["chg_pct"], reverse=True)
    top = signals[:free_slots]

    cash = kis_get_balance(app_key, app_secret, token, cano, prdt_cd)
    if cash <= 0:
        return state

    budget_each = cash / max(free_slots, 1)
    today_str = now.strftime("%Y-%m-%d")
    for s in top:
        qty = int(budget_each // s["price"])
        if qty < 1:
            continue
        result = kis_place_order(s["ticker"], qty, "buy", app_key, app_secret, token, cano, prdt_cd)
        msg = (f"[장중봇 매수]\n{s['name']} ({s['ticker']})\n"
               f"현재가: {s['price']:,.0f}원 (+{s['chg_pct']:.2f}%), {qty}주\n"
               f"주문결과: {result.get('msg1', result)}")
        print(msg)
        send_telegram(msg)
        state["positions"].append({
            "ticker": s["ticker"], "name": s["name"],
            "entry_price": s["price"], "peak_price": s["price"],
            "qty": qty, "entry_date": today_str,
        })

    return state


def run_bot():
    now = now_kst()
    if not is_market_hours(now):
        print(f"장 시간이 아닙니다 ({now.strftime('%Y-%m-%d %H:%M')} KST). 종료합니다.")
        return

    app_key, app_secret, cano, prdt_cd = get_kis_credentials()
    token = get_access_token(app_key, app_secret)

    state = load_state()
    force_close = is_force_close_time(now)

    if state["positions"]:
        state = check_positions(state, app_key, app_secret, token, cano, prdt_cd, force_close)

    if not force_close:
        state = try_enter_new_positions(state, app_key, app_secret, token, cano, prdt_cd, now)

    save_state(state)


if __name__ == "__main__":
    run_bot()
