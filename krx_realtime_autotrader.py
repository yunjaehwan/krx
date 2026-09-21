# -*- coding: utf-8 -*-
"""
KRX 장중 실시간 추격 자동매매 (정규장 전용, V4)
==================================================
GPT가 만든 V2/V3 프로토타입을 검토하면서 발견한 두 가지 실제 문제를
고친 버전입니다.

V2/V3에서 발견된 문제
----------------------
1. 주문 체결 확인이 "이 종목 오늘 체결 전부"를 가져와서 판단해서,
   같은 종목을 하루에 두 번 이상 거래하면 어떤 주문의 체결인지
   구분이 안 됐습니다.
   -> 이 버전은 주문을 넣을 때 받는 주문번호(ODNO)로 정확히 매칭합니다.

2. 매도 주문을 "넣는 순간" 바로 청산 처리하고 내부 기록에서
   지워버려서, 그 주문이 실제로 거부되면 계좌엔 주식이 남아있는데
   봇은 더 이상 감시를 안 하는 상태가 될 수 있었습니다.
   -> 이 버전은 실제 체결이 확인될 때까지 포지션을 계속 들고
      있으면서 "매도 시도 중" 표시만 해둡니다. 만약 60초 안에
      체결이 확인 안 되면 다시 매도를 시도합니다 (포지션을 잃지
      않습니다).

그리고 V3가 시도했던 "8시~20시(정규장+애프터마켓) 확장"은
이 버전에서 제외했습니다. 애프터마켓은 주문 방식(시장가 허용 여부),
유동성, 호가 구조가 정규장과 달라서 지금 조건을 그대로 쓰면
위험할 수 있고, 이번 대화에서 검증한 적이 없는 영역입니다.
그래서 이 버전은 정규장(09:00~15:30)만 다룹니다.

※ 여전히 검증된 전략은 아닙니다 (분봉 데이터로 과거 백테스트를
할 수 없는 실험적 전략). 모의투자로 충분히 지켜본 뒤 판단하세요.

사전 준비
---------
    pip install requests pandas finance-datareader websocket-client

환경변수
--------
    KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import sys
import time
import datetime
from pathlib import Path

import requests
import pandas as pd

try:
    import FinanceDataReader as fdr
except ImportError:
    fdr = None

from krx_realtime_ws import KISRealtimeWS, RealtimeBook


# ============================================================
# 설정값
# ============================================================

IS_MOCK = True  # 절대 True 로만 두세요.

SURGE_RATIO = 3.0
MIN_CHANGE_PCT = 5.0
MAX_CHANGE_PCT = 12.0

MIN_VOLUME_ACCEL_10S = 1.30
MIN_PRICE_CHANGE_10S = 0.10
SIGNAL_COOLDOWN_SEC = 60

TRAILING_STOP_PCT = 3.0
TOP_K = 2
MIN_MARKET_CAP = 100_000_000_000
PREFILTER_TOP_N = 100  # 웹소켓 동시구독 상한이 불확실해서 보수적으로 설정

MARKET_OPEN = "09:00"
MARKET_CLOSE = "15:30"
FORCE_CLOSE_TIME = "15:20"
PENDING_TIMEOUT_SEC = 60  # 이 시간 안에 체결 확인이 안 되면 재시도

STATE_DIR = "./krx_intraday_state"
LOG_DIR = "./krx_intraday_logs"
KST = datetime.timezone(datetime.timedelta(hours=9))

KIS_BASE_URL = ("https://openapivts.koreainvestment.com:29443" if IS_MOCK
                 else "https://openapi.koreainvestment.com:9443")
TR_BUY = "VTTC0012U" if IS_MOCK else "TTTC0012U"
TR_SELL = "VTTC0011U" if IS_MOCK else "TTTC0011U"
TR_BALANCE = "VTTC8434R" if IS_MOCK else "TTTC8434R"
TR_DAILY_CCLD = "VTTC0081R" if IS_MOCK else "TTTC0081R"


def now_kst():
    return datetime.datetime.now(KST)


# ============================================================
# KIS API
# ============================================================

def get_credentials():
    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    account_no = os.environ.get("KIS_ACCOUNT_NO")
    if not app_key or not app_secret or not account_no:
        raise RuntimeError("KIS_APP_KEY/KIS_APP_SECRET/KIS_ACCOUNT_NO 환경변수가 필요합니다.")
    cano, prdt_cd = account_no.split("-")
    return app_key, app_secret, cano, prdt_cd


def get_access_token(app_key, app_secret):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "kis_token.json")
    if os.path.exists(path):
        try:
            cached = json.loads(Path(path).read_text(encoding="utf-8"))
            exp = datetime.datetime.fromisoformat(cached["expire_at"])
            if now_kst() < exp:
                return cached["access_token"]
        except Exception:
            pass
    r = requests.post(
        f"{KIS_BASE_URL}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"토큰 발급 실패! 상태코드: {r.status_code}, 응답: {r.text}")
    r.raise_for_status()
    data = r.json()
    token = data["access_token"]
    exp = now_kst() + datetime.timedelta(hours=20)
    Path(path).write_text(json.dumps({"access_token": token, "expire_at": exp.isoformat()}), encoding="utf-8")
    return token


def get_hashkey(app_key, app_secret, body):
    r = requests.post(
        f"{KIS_BASE_URL}/uapi/hashkey",
        headers={"content-type": "application/json", "appkey": app_key, "appsecret": app_secret},
        json=body, timeout=10,
    )
    r.raise_for_status()
    return r.json()["HASH"]


def place_order(ticker, qty, side, app_key, app_secret, token, cano, prdt_cd):
    """시장가 주문, SOR(KRX/NXT 중 유리한 곳 자동 선택). rt_cd, ODNO 등 원본 응답 그대로 반환."""
    body = {
        "CANO": cano, "ACNT_PRDT_CD": prdt_cd,
        "PDNO": ticker, "ORD_DVSN": "01",
        "ORD_QTY": str(int(qty)), "ORD_UNPR": "0",
        "EXCG_ID_DVSN_CD": "SOR",
    }
    tr_id = TR_BUY if side == "buy" else TR_SELL
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": tr_id, "custtype": "P",
        "hashkey": get_hashkey(app_key, app_secret, body),
    }
    r = requests.post(f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
                       headers=headers, json=body, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"rt_cd": "-1", "msg1": f"응답 파싱 실패: {r.text[:200]}"}


def get_balance(app_key, app_secret, token, cano, prdt_cd):
    r = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers={
            "content-type": "application/json", "authorization": f"Bearer {token}",
            "appkey": app_key, "appsecret": app_secret, "tr_id": TR_BALANCE,
        },
        params={
            "CANO": cano, "ACNT_PRDT_CD": prdt_cd,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        },
        timeout=10,
    )
    data = r.json()
    try:
        return float(data["output2"][0]["dnca_tot_amt"])
    except Exception:
        return 0.0


def get_today_fills(app_key, app_secret, token, cano, prdt_cd):
    """오늘 전체 주문/체결 내역을 한 번에 가져온다 (종목별로 나눠 부르지 않음)."""
    today = now_kst().strftime("%Y%m%d")
    params = {
        "CANO": cano, "ACNT_PRDT_CD": prdt_cd,
        "INQR_STRT_DT": today, "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00",
        "PDNO": "", "CCLD_DVSN": "00",
        "ORD_GNO_BRNO": "", "ODNO": "",
        "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "EXCG_ID_DVSN_CD": "SOR",
    }
    r = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
        headers={
            "content-type": "application/json", "authorization": f"Bearer {token}",
            "appkey": app_key, "appsecret": app_secret, "tr_id": TR_DAILY_CCLD,
        },
        params=params, timeout=10,
    )
    try:
        return r.json().get("output1") or []
    except Exception:
        return []


# ============================================================
# 순수 로직 (테스트 가능한 부분) — KIS 응답 파싱과 전략 판단
# ============================================================

def find_fill_for_odno(fill_rows, odno):
    """주문번호(odno)로 정확히 매칭되는 체결 정보를 찾는다.
    return: (filled_qty, avg_price) 또는 None (아직 체결 없음/못 찾음)
    """
    for row in fill_rows:
        if str(row.get("odno", "")) != str(odno):
            continue
        qty = int(float(row.get("tot_ccld_qty", 0) or 0))
        if qty <= 0:
            return None
        price = float(row.get("avg_prvs", row.get("avg_ccld_unpr", row.get("avg_ccld_prc", 0))) or 0)
        if price <= 0:
            return None
        return qty, price
    return None


def signal_ok(ticker, row, baseline_row, book,
              surge_ratio=SURGE_RATIO, min_chg=MIN_CHANGE_PCT, max_chg=MAX_CHANGE_PCT,
              min_accel=MIN_VOLUME_ACCEL_10S, min_price_chg_10s=MIN_PRICE_CHANGE_10S):
    """실시간 스냅샷(row)과 전일 기준 데이터(baseline_row)로 매수 신호 판단."""
    prev_close = float(baseline_row["전일종가"])
    prev_vol = float(baseline_row["전일거래량"])
    price = float(row.get("price", 0))
    if prev_close <= 0 or prev_vol <= 0 or price <= 0:
        return False, {}

    chg = (price - prev_close) / prev_close * 100.0
    vol_ratio = row.get("cumulative_volume", 0) / prev_vol
    v10 = book.volume_window(ticker, 10)
    v20 = book.volume_window(ticker, 20)
    prev10 = max(0.0, v20 - v10)
    accel = v10 / max(prev10, 1.0)
    p10 = book.price_change_window(ticker, 10)
    vwap = row.get("vwap", 0)

    ok = (
        min_chg <= chg <= max_chg
        and vol_ratio >= surge_ratio
        and accel >= min_accel
        and p10 >= min_price_chg_10s
        and (vwap <= 0 or price >= vwap)  # VWAP 값이 아직 없으면(0) 필터를 걸지 않음
    )
    metrics = {
        "ticker": ticker, "price": price, "change_pct": chg,
        "volume_ratio": vol_ratio, "volume_10s": v10,
        "volume_accel_10s": accel, "price_change_10s": p10,
        "vwap": vwap, "signal": "BUY" if ok else "PASS",
        "timestamp": now_kst().isoformat(),
    }
    return ok, metrics


def is_market_hours(now):
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return MARKET_OPEN <= hm <= MARKET_CLOSE


def is_force_close_time(now):
    return now.strftime("%H:%M") >= FORCE_CLOSE_TIME


# ============================================================
# 후보 종목
# ============================================================

def build_baseline(date_str):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"baseline_{date_str}.csv")
    if os.path.exists(path):
        cached = pd.read_csv(path, dtype={"종목코드": str}).set_index("종목코드")
        if len(cached) > 0:
            return cached

    if fdr is None:
        raise RuntimeError("FinanceDataReader가 설치되어 있지 않습니다.")

    df = fdr.StockListing("KRX")
    rename = {}
    for c in df.columns:
        lc = str(c).lower()
        if lc == "code": rename[c] = "종목코드"
        elif lc == "name": rename[c] = "종목명"
        elif lc == "close": rename[c] = "전일종가"
        elif lc == "volume": rename[c] = "전일거래량"
        elif lc == "marcap": rename[c] = "시가총액"
    df = df.rename(columns=rename)

    cols = ["종목코드", "종목명", "전일종가", "전일거래량", "시가총액"]
    df = df[[c for c in cols if c in df.columns]].copy()
    for c in ["전일종가", "전일거래량", "시가총액"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["종목코드", "전일종가", "전일거래량"])
    if "시가총액" in df.columns and df["시가총액"].notna().sum() > 0:
        df = df[df["시가총액"] >= MIN_MARKET_CAP]
    df["전일거래대금"] = df["전일종가"] * df["전일거래량"]
    df = df.sort_values("전일거래대금", ascending=False).head(PREFILTER_TOP_N)
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)

    if len(df) == 0:
        return df.set_index("종목코드") if "종목코드" in df.columns else pd.DataFrame()

    df.set_index("종목코드").to_csv(path, encoding="utf-8-sig")
    return df.set_index("종목코드")


# ============================================================
# 상태 관리 / 텔레그램
# ============================================================

def load_state():
    path = os.path.join(STATE_DIR, "positions.json")
    if os.path.exists(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"positions": [], "pending_buys": [], "last_signal": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    Path(os.path.join(STATE_DIR, "positions.json")).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       data={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")


def append_signal_log(row):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"signals_{now_kst():%Y%m%d}.csv")
    df = pd.DataFrame([row])
    header = not os.path.exists(path)
    df.to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")


# ============================================================
# 매수/매도 흐름
#   포지션 상태: 'active'(정상 보유) / 'selling'(매도 주문 냄, 체결확인 대기)
#   pending_buys: 매수 주문 낸 것들, 체결확인 대기
# ============================================================

def reconcile(state, app_key, app_secret, token, cano, prdt_cd):
    """오늘 체결 내역을 한 번에 가져와서, 대기 중인 매수/매도를 정리한다."""
    has_pending = state["pending_buys"] or any(p.get("sell_odno") for p in state["positions"])
    if not has_pending:
        return
    fills = get_today_fills(app_key, app_secret, token, cano, prdt_cd)

    # 매수 체결 확인
    still_pending = []
    for p in state["pending_buys"]:
        found = find_fill_for_odno(fills, p["odno"])
        if found:
            qty, avg = found
            state["positions"].append({
                "ticker": p["ticker"], "name": p["name"],
                "entry_price": avg, "peak_price": avg, "qty": qty,
                "entry_date": now_kst().strftime("%Y-%m-%d"),
            })
            send_telegram(f"[실제 체결-매수]\n{p['name']} ({p['ticker']})\n체결가: {avg:,.0f}원 / {qty}주")
        elif time.time() - p["submitted_at"] > PENDING_TIMEOUT_SEC:
            send_telegram(f"[매수 미체결 포기]\n{p['name']} ({p['ticker']})\n{PENDING_TIMEOUT_SEC}초 내 체결 확인 안 됨, 포기합니다.")
        else:
            still_pending.append(p)
    state["pending_buys"] = still_pending

    # 매도 체결 확인
    remaining_positions = []
    for pos in state["positions"]:
        odno = pos.get("sell_odno")
        if not odno:
            remaining_positions.append(pos)
            continue
        found = find_fill_for_odno(fills, odno)
        if found:
            qty, avg = found
            pnl = (avg - pos["entry_price"]) / pos["entry_price"] * 100
            send_telegram(
                f"[실제 체결-매도]\n{pos['name']} ({pos['ticker']})\n"
                f"매수가: {pos['entry_price']:,.0f}원 -> 체결가: {avg:,.0f}원 ({pnl:+.2f}%)"
            )
            # 포지션 완전히 제거 (remaining_positions에 안 넣음)
        elif time.time() - pos.get("sell_submitted_at", 0) > PENDING_TIMEOUT_SEC:
            # 시간 초과: 매도 실패로 보고 포지션을 되살려서 다음 루프에서 재시도
            pos.pop("sell_odno", None)
            pos.pop("sell_submitted_at", None)
            print(f"[매도 재시도] {pos['ticker']} {PENDING_TIMEOUT_SEC}초 내 체결 미확인, 다시 시도합니다.")
            remaining_positions.append(pos)
        else:
            remaining_positions.append(pos)  # 아직 대기 중
    state["positions"] = remaining_positions


def check_positions(state, book, app_key, app_secret, token, cano, prdt_cd):
    force = is_force_close_time(now_kst())
    for pos in state["positions"]:
        if pos.get("sell_odno"):
            continue  # 이미 매도 시도 중, 중복 주문 방지

        row = book.snapshot(pos["ticker"])
        price = row.get("price", 0)
        if price <= 0:
            continue

        pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
        drop = (price - pos["peak_price"]) / pos["peak_price"] * 100
        pnl = (price - pos["entry_price"]) / pos["entry_price"] * 100

        should_sell = drop <= -TRAILING_STOP_PCT or force
        if not should_sell:
            continue

        reason = "추적매도" if drop <= -TRAILING_STOP_PCT else "장마감 강제매도"
        result = place_order(pos["ticker"], pos["qty"], "sell", app_key, app_secret, token, cano, prdt_cd)
        if result.get("rt_cd") == "0":
            odno = result.get("output", {}).get("ODNO", "")
            pos["sell_odno"] = odno
            pos["sell_submitted_at"] = time.time()
            msg = (f"[장중봇 매도신호-{reason}]\n{pos['name']} ({pos['ticker']})\n"
                   f"매수가: {pos['entry_price']:,.0f}원 -> 현재가: {price:,.0f}원 ({pnl:+.2f}%)\n"
                   f"고점대비: {drop:+.2f}%\n주문 접수됨(체결 확인 대기)")
        else:
            # 주문 자체가 거부됨: 포지션은 그대로 두고(위 continue로 넘어가지 않았으므로
            # 다음 루프에서 조건이 계속 참이면 자동으로 재시도됨) 알림만 보낸다.
            msg = (f"[장중봇 매도 주문 실패-{reason}]\n{pos['name']} ({pos['ticker']})\n"
                   f"사유: {result.get('msg1', result)}\n다음 루프에서 재시도합니다.")
        print(msg)
        send_telegram(msg)
        time.sleep(1)


def try_enter(state, baseline, book, app_key, app_secret, token, cano, prdt_cd):
    occupied = len(state["positions"]) + len(state["pending_buys"])
    slots = TOP_K - occupied
    if slots <= 0:
        return

    held_codes = {p["ticker"] for p in state["positions"] + state["pending_buys"]}
    candidates = []
    for ticker, b in baseline.iterrows():
        if ticker in held_codes:
            continue
        row = book.snapshot(ticker)
        if not row:
            continue

        ok, m = signal_ok(ticker, row, b, book)
        if m:
            append_signal_log({**m, "name": b["종목명"]})
        if not ok:
            continue

        last = state["last_signal"].get(ticker, 0)
        if time.time() - last < SIGNAL_COOLDOWN_SEC:
            continue
        state["last_signal"][ticker] = time.time()
        candidates.append({**m, "name": b["종목명"]})

    if not candidates:
        return
    candidates.sort(key=lambda x: (x["volume_accel_10s"], x["price_change_10s"], x["change_pct"]), reverse=True)

    cash = get_balance(app_key, app_secret, token, cano, prdt_cd)
    if cash <= 0:
        return
    budget = cash / max(slots, 1)

    for s in candidates[:slots]:
        qty = int(budget // s["price"])
        if qty < 1:
            continue
        result = place_order(s["ticker"], qty, "buy", app_key, app_secret, token, cano, prdt_cd)
        if result.get("rt_cd") == "0":
            odno = result.get("output", {}).get("ODNO", "")
            state["pending_buys"].append({
                "ticker": s["ticker"], "name": s["name"], "qty": qty,
                "odno": odno, "submitted_at": time.time(),
            })
            msg = (f"[실시간 매수신호]\n{s['name']} ({s['ticker']})\n"
                   f"현재가: {s['price']:,.0f}원 ({s['change_pct']:+.2f}%)\n"
                   f"거래량배수: {s['volume_ratio']:.2f}x / 10초 거래량가속: {s['volume_accel_10s']:.2f}x\n"
                   f"주문: {qty}주 (체결 확인 대기)")
        else:
            msg = f"[매수 주문 실패]\n{s['name']} ({s['ticker']})\n사유: {result.get('msg1', result)}"
        print(msg)
        send_telegram(msg)
        time.sleep(1)


# ============================================================
# 메인
# ============================================================

def main():
    if not IS_MOCK:
        raise RuntimeError("안전상 이 파일은 IS_MOCK=True만 허용합니다.")

    now = now_kst()
    if not is_market_hours(now):
        print(f"장 시간이 아닙니다: {now.strftime('%Y-%m-%d %H:%M')} KST")
        return

    app_key, app_secret, cano, prdt_cd = get_credentials()
    token = get_access_token(app_key, app_secret)

    baseline = build_baseline(now.strftime("%Y%m%d"))
    if baseline is None or len(baseline) == 0:
        raise RuntimeError("후보 종목 baseline 생성 실패")

    state = load_state()
    book = RealtimeBook()

    ws = KISRealtimeWS(app_key, app_secret, is_mock=IS_MOCK, book=book)
    ws.add_symbols(baseline.index.tolist())
    ws.start()

    print(f"[START] 실시간 WebSocket 후보 {len(baseline)}종목")
    send_telegram(f"[장중봇 V4 시작] WebSocket {len(baseline)}종목 구독")

    try:
        time.sleep(5)  # 초기 데이터 쌓일 시간
        while now_kst().strftime("%H:%M") <= MARKET_CLOSE:
            if not ws.connected.is_set():
                print("[WS] 연결 대기 중...")
                time.sleep(1)
                continue

            reconcile(state, app_key, app_secret, token, cano, prdt_cd)
            check_positions(state, book, app_key, app_secret, token, cano, prdt_cd)
            if now_kst().strftime("%H:%M") < FORCE_CLOSE_TIME:
                try_enter(state, baseline, book, app_key, app_secret, token, cano, prdt_cd)

            save_state(state)
            time.sleep(0.25)

    except KeyboardInterrupt:
        print("사용자 중단")
    finally:
        ws.stop()
        save_state(state)
        print("[END] WebSocket 종료")


if __name__ == "__main__":
    main()
