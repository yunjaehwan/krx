# -*- coding: utf-8 -*-
"""
KIS 국내주식 실시간 WebSocket 데이터 모듈
==========================================
- H0UNCNT0: KRX+NXT 통합 실시간 체결
- 자동 재접속 (지수 백오프)
- 종목별 최근 체결량/가격 버퍼 제공 (최근 N초 거래량 가속도, 가격 변화 계산용)

필요 패키지:
    pip install websocket-client requests

참고: 한 연결이 동시에 구독할 수 있는 종목 수에 상한이 있을 수 있다는
얘기가 있으나 정확한 현재 값은 공식 문서로 확인하지 못했습니다.
후보 종목 수(PREFILTER_TOP_N, 실행 파일 쪽 설정)를 보수적으로 잡고,
실제로 몇 종목까지 정상 수신되는지 로그로 확인하는 걸 권장합니다.
"""
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import requests

try:
    import websocket
except ImportError:
    websocket = None


KST = timezone(timedelta(hours=9))
MOCK_WS_URL = "ws://ops.koreainvestment.com:31000"
REAL_WS_URL = "ws://ops.koreainvestment.com:21000"

TRADE_TR_ID = "H0UNCNT0"  # KRX+NXT 통합 실시간 체결

# H0UNCNT0 공식 필드 순서 (체결 데이터가 파이프/캐럿으로 구분되어 옴)
PRICE_FIELDS = [
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN",
    "PRDY_VRSS", "PRDY_CTRT", "WGHN_AVRG_STCK_PRC", "STCK_OPRC",
    "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL", "ACML_VOL",
    "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU",
    "CTTR", "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE",
    "PRDY_VOL_VRSS_ACML_VOL_RATE", "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN",
    "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN", "HGPR_VRSS_PRPR",
    "LWPR_HOUR", "LWPR_VRSS_PRPR", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1",
    "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "VOL_TNRT",
    "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE",
    "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
]


def _f(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return default


def get_approval_key(app_key: str, app_secret: str, is_mock: bool) -> str:
    base = ("https://openapivts.koreainvestment.com:29443" if is_mock
            else "https://openapi.koreainvestment.com:9443")
    r = requests.post(
        f"{base}/oauth2/Approval",
        headers={"content-type": "application/json"},
        json={"grant_type": "client_credentials", "appkey": app_key, "secretkey": app_secret},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("approval_key"):
        raise RuntimeError(f"WebSocket approval key 발급 실패: {data}")
    return data["approval_key"]


class RealtimeBook:
    """전략에서 읽는 종목별 실시간 상태. 스레드 세이프."""

    def __init__(self, max_ticks=600):
        self.lock = threading.RLock()
        self.data = {}
        self.ticks = defaultdict(lambda: deque(maxlen=max_ticks))

    def update_trade(self, row):
        code = row.get("MKSC_SHRN_ISCD", "")
        if not code:
            return
        now = time.time()
        price = _f(row.get("STCK_PRPR"))
        acml_vol = _f(row.get("ACML_VOL"))
        tick_vol = _f(row.get("CNTG_VOL"))
        trade_value = _f(row.get("ACML_TR_PBMN"))
        change_pct = _f(row.get("PRDY_CTRT"))
        vwap = _f(row.get("WGHN_AVRG_STCK_PRC"))
        ask = _f(row.get("ASKP1"))
        bid = _f(row.get("BIDP1"))

        with self.lock:
            old = self.data.get(code, {})
            self.data[code] = {
                **old,
                "ticker": code,
                "timestamp": now,
                "trade_time": row.get("STCK_CNTG_HOUR", ""),
                "price": price,
                "change_pct": change_pct,
                "tick_volume": tick_vol,
                "cumulative_volume": acml_vol,
                "cumulative_trade_value": trade_value,
                "vwap": vwap,
                "ask_price": ask,
                "bid_price": bid,
            }
            self.ticks[code].append((now, price, tick_vol, acml_vol))

    def snapshot(self, ticker):
        with self.lock:
            return dict(self.data.get(ticker, {}))

    def volume_window(self, ticker, seconds):
        cutoff = time.time() - seconds
        with self.lock:
            ticks = list(self.ticks.get(ticker, ()))
        recent = [x for x in ticks if x[0] >= cutoff]
        if len(recent) < 2:
            return sum(x[2] for x in recent)
        return max(0.0, recent[-1][3] - recent[0][3])

    def price_change_window(self, ticker, seconds):
        cutoff = time.time() - seconds
        with self.lock:
            ticks = list(self.ticks.get(ticker, ()))
        recent = [x for x in ticks if x[0] >= cutoff]
        if len(recent) < 2:
            return 0.0
        first, last = recent[0][1], recent[-1][1]
        if first <= 0:
            return 0.0
        return (last - first) / first * 100.0


class KISRealtimeWS:
    def __init__(self, app_key, app_secret, is_mock=True, book=None,
                 reconnect_min=2, reconnect_max=30):
        if websocket is None:
            raise RuntimeError("websocket-client 패키지가 필요합니다: pip install websocket-client")
        self.app_key = app_key
        self.app_secret = app_secret
        self.is_mock = is_mock
        self.book = book or RealtimeBook()
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.stock_codes = set()
        self.running = False
        self.ws = None
        self.thread = None
        self.connected = threading.Event()
        self.last_message_at = 0.0

    def add_symbols(self, symbols):
        self.stock_codes.update(str(x).zfill(6) for x in symbols)

    def _message(self, tr_id, tr_key, tr_type="1", approval_key=None):
        return json.dumps({
            "header": {
                "approval_key": approval_key, "custtype": "P",
                "tr_type": tr_type, "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        })

    def _on_open(self, ws):
        self.connected.set()
        approval = get_approval_key(self.app_key, self.app_secret, self.is_mock)
        for code in sorted(self.stock_codes):
            ws.send(self._message(TRADE_TR_ID, code, "1", approval))
            time.sleep(0.03)
        print(f"[WS] 연결/체결 구독 완료: {len(self.stock_codes)}종목")

    def _parse_trade(self, raw):
        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != TRADE_TR_ID:
            return
        rows = parts[3].split("^")
        if len(rows) < len(PRICE_FIELDS):
            return
        row = dict(zip(PRICE_FIELDS, rows[:len(PRICE_FIELDS)]))
        self.book.update_trade(row)

    def _on_message(self, ws, message):
        self.last_message_at = time.time()
        if not message:
            return
        if message.startswith("0|"):
            self._parse_trade(message)
            return
        if message.startswith("1|"):
            return
        try:
            obj = json.loads(message)
            tr_id = obj.get("header", {}).get("tr_id")
            if tr_id == "PINGPONG":
                ws.send(message)
            else:
                msg = obj.get("body", {}).get("msg1", "")
                if msg:
                    print(f"[WS] {tr_id}: {msg}")
        except Exception:
            pass

    def _on_error(self, ws, error):
        print(f"[WS] 오류: {error}")

    def _on_close(self, ws, code, msg):
        self.connected.clear()
        print(f"[WS] 종료: {code} {msg}")

    def _run(self):
        delay = self.reconnect_min
        while self.running:
            try:
                url = MOCK_WS_URL if self.is_mock else REAL_WS_URL
                self.ws = websocket.WebSocketApp(
                    url, on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10, ping_payload="KIS")
                delay = self.reconnect_min
            except Exception as e:
                print(f"[WS] 재접속 예외: {e}")
            if self.running:
                time.sleep(delay)
                delay = min(self.reconnect_max, delay * 2)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.connected.clear()
