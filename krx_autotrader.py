# -*- coding: utf-8 -*-
"""
KRX 자동매매 봇 (한국투자증권 모의투자 연동)
================================================
지금까지 검증한 전략을 실제로 자동매매합니다.

전략 (검증된 그대로 고정)
--------------------------
    - 신호: 전일 대비 거래량 3배 이상 + 당일 상승률 5~25%
    - 그날 조건 맞는 종목 중 상승률 강한 상위 TOP_K개를 동시호가
      시간(15:20~15:29)에 균등 분할 매수
    - 다음 거래일: 손절(-3%) 또는 추적익절(+5% 찍고 고점대비 -3%)
      걸리면 그 즉시 매도, 안 걸리면 그날 동시호가에 매도
    - TOP_K개가 전부 정리되어야 다음 사이클 진입 (동시 회전 방식)

동작 시간
---------
    평일 9:00~15:30, 5분마다 실행 (GitHub Actions 스케줄)
    - 9:00~15:19  : 보유 종목 손절/추적익절 조건만 확인
    - 15:20~15:29 : 오늘 만기인 종목 강제 종가 매도 + (자금이 비어있으면) 새 신호로 진입

중요: 처음엔 모의투자(가상 계좌)로만 동작하도록 IS_MOCK=True 로
고정되어 있습니다. 절대 임의로 False로 바꾸지 마세요.

사전 준비
---------
    pip install requests pandas finance-datareader

환경변수 (GitHub Secrets)
---------
    KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO (예: "50206458-01")
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

디버그
------
    python krx_autotrader.py --debug-price 005930   # KIS 현재가 조회 테스트
    python krx_autotrader.py --debug-balance         # 잔고 조회 테스트
"""

import sys
import os
import json
import time
import datetime

import requests
import pandas as pd

try:
    import FinanceDataReader as fdr
except ImportError:
    print("FinanceDataReader가 설치되어 있지 않습니다. pip install finance-datareader")
    sys.exit(1)


# ============================================================
# 설정값
# ============================================================

IS_MOCK = True  # 절대 True 로만 두세요. 실전 전환은 별도로 논의 후 진행합니다.

SURGE_RATIO = 3.0
MIN_CHANGE_PCT = 5.0
MAX_CHANGE_PCT = 25.0
TRAIL_ACTIVATE_PCT = 5.0
STOP_LOSS_PCT = -3.0
TRAILING_STOP_PCT = 3.0
TOP_K = 2

MIN_MARKET_CAP = 100_000_000_000  # 1,000억원
PREFILTER_TOP_N = 300

CLOSING_WINDOW_START = "15:20"
CLOSING_WINDOW_END = "15:29"

STATE_DIR = "./krx_autotrader_state"
HEADERS_NAVER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KST = datetime.timezone(datetime.timedelta(hours=9))

KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_MOCK else "https://openapi.koreainvestment.com:9443"

TR_BUY = "VTTC0802U" if IS_MOCK else "TTTC0802U"
TR_SELL = "VTTC0801U" if IS_MOCK else "TTTC0801U"
TR_BALANCE = "VTTC8434R" if IS_MOCK else "TTTC8434R"
TR_PRICE = "FHKST01010100"


# ============================================================
# KIS API 인증
# ============================================================

def get_kis_credentials():
    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    account_no = os.environ.get("KIS_ACCOUNT_NO")
    if not app_key or not app_secret or not account_no:
        print("KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 환경변수가 없습니다.")
        sys.exit(1)
    cano, prdt_cd = account_no.split("-")
    return app_key, app_secret, cano, prdt_cd


def get_access_token(app_key, app_secret):
    """토큰은 하루 유효하므로 파일에 캐시해서 재사용합니다."""
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
    res.raise_for_status()
    data = res.json()
    token = data["access_token"]
    expire_at = datetime.datetime.now(KST) + datetime.timedelta(hours=20)  # 여유있게 20시간만 사용
    with open(token_path, "w", encoding="utf-8") as f:
        json.dump({"access_token": token, "expire_at": expire_at.isoformat()}, f)
    return token


def get_hashkey(app_key, app_secret, body):
    url = f"{KIS_BASE_URL}/uapi/hashkey"
    headers = {"content-type": "application/json", "appkey": app_key, "appsecret": app_secret}
    res = requests.post(url, headers=headers, json=body, timeout=10)
    res.raise_for_status()
    return res.json()["HASH"]


# ============================================================
# KIS API 호출
# ============================================================

def kis_get_price(ticker, app_key, app_secret, token):
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": TR_PRICE,
    }
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    res = requests.get(url, headers=headers, params=params, timeout=10)
    data = res.json()
    try:
        return float(data["output"]["stck_prpr"])
    except Exception:
        return None


def kis_get_balance(app_key, app_secret, token, cano, prdt_cd):
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": TR_BALANCE,
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
        cash = float(data["output2"][0]["dnca_tot_amt"])  # 예수금
    except Exception:
        pass
    return cash, data


def kis_place_order(ticker, qty, side, app_key, app_secret, token, cano, prdt_cd):
    """side: 'buy' 또는 'sell'. 시장가 주문(동시호가에서도 동작)."""
    tr_id = TR_BUY if side == "buy" else TR_SELL
    body = {
        "CANO": cano, "ACNT_PRDT_CD": prdt_cd,
        "PDNO": ticker, "ORD_DVSN": "01",  # 01=시장가
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


def is_closing_window(now):
    hm = now.strftime("%H:%M")
    return CLOSING_WINDOW_START <= hm <= CLOSING_WINDOW_END


# ============================================================
# 후보 종목 (자가치유 캐시, krx_realtime_alert.py 와 동일 패턴)
# ============================================================

def build_or_load_baseline(date_str):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"baseline_{date_str}.csv")
    if os.path.exists(path):
        cached = pd.read_csv(path, dtype={"종목코드": str}).set_index("종목코드")
        if len(cached) > 0:
            return cached
        print("캐시된 후보가 0개라 다시 만듭니다...")

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
# 상태 관리 (보유 포지션)
# ============================================================

def load_state():
    path = os.path.join(STATE_DIR, "positions.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": [], "last_entry_date": None}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "positions.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
# 텔레그램
# ============================================================

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 환경변수 없음, 전송 생략")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")


# ============================================================
# 메인 로직
# ============================================================

def check_and_exit_positions(state, app_key, app_secret, token, cano, prdt_cd, now, closing):
    today_str = now.strftime("%Y-%m-%d")
    remaining = []
    for pos in state["positions"]:
        price, _ = fetch_realtime_quote(pos["ticker"])
        if price is None:
            remaining.append(pos)
            continue

        pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
        change_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
        peak_change_pct = (pos["peak_price"] - pos["entry_price"]) / pos["entry_price"] * 100

        should_sell = False
        reason = ""

        if change_pct <= STOP_LOSS_PCT:
            should_sell, reason = True, "손절"
        elif peak_change_pct >= TRAIL_ACTIVATE_PCT:
            drop_from_peak = (price - pos["peak_price"]) / pos["peak_price"] * 100
            if drop_from_peak <= -TRAILING_STOP_PCT:
                should_sell, reason = True, "추적익절"
        elif closing and pos["entry_date"] < today_str:
            should_sell, reason = True, "종가매도"

        if should_sell:
            result = kis_place_order(pos["ticker"], pos["qty"], "sell", app_key, app_secret, token, cano, prdt_cd)
            msg = (f"[매도-{reason}]\n{pos['name']} ({pos['ticker']})\n"
                   f"매수가: {pos['entry_price']:,.0f}원 -> 현재가: {price:,.0f}원 ({change_pct:+.2f}%)\n"
                   f"주문결과: {result.get('msg1', result)}")
            print(msg)
            send_telegram(msg)
        else:
            remaining.append(pos)

    state["positions"] = remaining
    return state


def try_enter_new_positions(state, app_key, app_secret, token, cano, prdt_cd, now):
    today_str = now.strftime("%Y-%m-%d")
    if state["positions"]:
        return state  # 아직 자리가 안 비었음
    if state.get("last_entry_date") == today_str:
        return state  # 오늘 이미 진입함

    date_str = now.strftime("%Y%m%d")
    baseline = build_or_load_baseline(date_str)
    if len(baseline) == 0:
        print("후보 종목 0개, 진입 건너뜀")
        return state

    print(f"{len(baseline)}개 후보 실시간 확인 중...")
    signals = []
    for ticker, row in baseline.iterrows():
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
        print("오늘 신규 진입 신호 없음")
        return state

    signals.sort(key=lambda x: x["chg_pct"], reverse=True)
    top = signals[:TOP_K]

    cash, _ = kis_get_balance(app_key, app_secret, token, cano, prdt_cd)
    if cash <= 0:
        print("예수금 조회 실패 또는 0원, 진입 건너뜀")
        return state

    budget_each = cash / len(top)
    new_positions = []
    for s in top:
        qty = int(budget_each // s["price"])
        if qty < 1:
            print(f"{s['name']} 배정금액으로 1주도 못 사서 건너뜀")
            continue
        result = kis_place_order(s["ticker"], qty, "buy", app_key, app_secret, token, cano, prdt_cd)
        msg = (f"[매수]\n{s['name']} ({s['ticker']})\n"
               f"현재가: {s['price']:,.0f}원 (+{s['chg_pct']:.2f}%), {qty}주\n"
               f"주문결과: {result.get('msg1', result)}")
        print(msg)
        send_telegram(msg)
        new_positions.append({
            "ticker": s["ticker"], "name": s["name"],
            "entry_price": s["price"], "peak_price": s["price"],
            "qty": qty, "entry_date": today_str,
        })

    state["positions"] = new_positions
    state["last_entry_date"] = today_str
    return state


def run_bot():
    now = now_kst()
    if not is_market_hours(now):
        print(f"장 시간이 아닙니다 ({now.strftime('%Y-%m-%d %H:%M')} KST). 종료합니다.")
        return

    app_key, app_secret, cano, prdt_cd = get_kis_credentials()
    token = get_access_token(app_key, app_secret)

    state = load_state()
    closing = is_closing_window(now)

    if state["positions"]:
        state = check_and_exit_positions(state, app_key, app_secret, token, cano, prdt_cd, now, closing)

    if closing and not state["positions"]:
        state = try_enter_new_positions(state, app_key, app_secret, token, cano, prdt_cd, now)

    save_state(state)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--debug-price":
        app_key, app_secret, cano, prdt_cd = get_kis_credentials()
        token = get_access_token(app_key, app_secret)
        print(kis_get_price(sys.argv[2], app_key, app_secret, token))
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--debug-balance":
        app_key, app_secret, cano, prdt_cd = get_kis_credentials()
        token = get_access_token(app_key, app_secret)
        cash, raw = kis_get_balance(app_key, app_secret, token, cano, prdt_cd)
        print("예수금:", cash)
        print(json.dumps(raw, ensure_ascii=False, indent=2)[:2000])
        return
    run_bot()


if __name__ == "__main__":
    main()
