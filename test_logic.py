# -*- coding: utf-8 -*-
"""krx_realtime_autotrader.py 의 핵심 로직을 KIS 서버 없이 검증하는 테스트."""
import sys
import time
import pandas as pd

sys.path.insert(0, ".")
import krx_realtime_autotrader as bot
from krx_realtime_ws import RealtimeBook

PASS = 0
FAIL = 0


def check(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# ============================================================
# 1) signal_ok: 신호 판단 로직
# ============================================================
print("\n[1] signal_ok 테스트")

baseline_row = pd.Series({"전일종가": 10000.0, "전일거래량": 1_000_000.0, "종목명": "테스트전자"})

book = RealtimeBook()
now = time.time()
# 최근 10초 안에 거래량이 확 늘고 있는 상황(가속)을 흉내냄
book.ticks["005930"].extend([
    (now - 18, 10500, 1000, 3_000_000),  # 10~20초 구간 시작점
    (now - 9,  10600, 2000, 3_020_000),  # 최근 10초 구간 시작점
    (now - 1,  10700, 3000, 3_150_000),  # 지금
])
book.data["005930"] = {"price": 10700, "cumulative_volume": 3_150_000, "vwap": 10600}

ok, m = bot.signal_ok("005930", book.snapshot("005930"), baseline_row, book)
check("정상 급등 상황 -> 매수 신호 True", ok is True)
check("상승률 계산 정확 (7.0%)", abs(m["change_pct"] - 7.0) < 0.01)
check("거래량배수 계산 정확 (3.15배)", abs(m["volume_ratio"] - 3.15) < 0.01)

# 상승률이 범위를 벗어난 경우 (너무 많이 오름)
book2 = RealtimeBook()
book2.ticks["000001"].extend([(now - 10, 14000, 1000, 3_500_000), (now - 1, 14000, 1000, 3_500_000)])
book2.data["000001"] = {"price": 14000, "cumulative_volume": 3_500_000, "vwap": 13000}
row2 = book2.snapshot("000001")
ok2, _ = bot.signal_ok("000001", row2, baseline_row, book2)
check("상승률 40% (범위 밖) -> 매수 신호 False", ok2 is False)

# 거래량이 부족한 경우
book3 = RealtimeBook()
book3.ticks["000002"].extend([(now - 10, 10700, 100, 1_200_000), (now - 1, 10700, 100, 1_200_000)])
book3.data["000002"] = {"price": 10700, "cumulative_volume": 1_200_000, "vwap": 10000}
row3 = book3.snapshot("000002")
ok3, _ = bot.signal_ok("000002", row3, baseline_row, book3)
check("거래량 1.2배 (3배 미만) -> 매수 신호 False", ok3 is False)

# VWAP 아래인 경우
book4 = RealtimeBook()
book4.ticks["000003"].extend([(now - 10, 10700, 1000, 3_200_000), (now - 1, 10700, 1000, 3_200_000)])
book4.data["000003"] = {"price": 10700, "cumulative_volume": 3_200_000, "vwap": 11000}
row4 = book4.snapshot("000003")
ok4, _ = bot.signal_ok("000003", row4, baseline_row, book4)
check("VWAP(11000)보다 낮은 현재가(10700) -> 매수 신호 False", ok4 is False)


# ============================================================
# 2) RealtimeBook: volume_window / price_change_window
# ============================================================
print("\n[2] RealtimeBook 윈도우 계산 테스트")

b = RealtimeBook()
t0 = time.time() - 40  # 여유 있게 40초 전부터 시작 (경계 오차 방지)
b.ticks["005930"].extend([
    (t0, 10000, 100, 1_000_000),
    (t0 + 10, 10100, 100, 1_050_000),   # 30초 전 시점, 누적 1,050,000
    (t0 + 20, 10300, 100, 1_150_000),   # 20초 전 시점(=지금부터 20초 윈도 안), 누적 1,150,000
    (t0 + 40, 10500, 100, 1_300_000),   # 지금,      누적 1,300,000
])
v10 = b.volume_window("005930", 15)   # 최근 15초: 지금 틱(t0+40)만 해당 -> 자기 자신만이라 0 처리 로직 확인용
v20 = b.volume_window("005930", 25)   # 최근 25초: t0+20(1,150,000) ~ t0+40(1,300,000) = 150,000
check("최근 25초 거래량 = 150,000 (t0+20 ~ t0+40)", abs(v20 - 150_000) < 1)

p20 = b.price_change_window("005930", 25)  # (10500-10300)/10300*100, t0+20 -> t0+40
check(f"최근 25초 가격변화율 계산 정확 (실제 {p20:.3f}%)", abs(p20 - ((10500 - 10300) / 10300 * 100)) < 0.01)


# ============================================================
# 3) find_fill_for_odno: 주문번호로 정확히 매칭하는지 (핵심 버그 수정 검증)
# ============================================================
print("\n[3] find_fill_for_odno 테스트 (같은 종목 하루 두 번 거래 시나리오)")

# 시나리오: 005930을 오늘 매수(ODNO=1001)했다가 손절매도(ODNO=1002)하고,
# 다시 매수(ODNO=1003)한 상황. 오늘 체결 내역엔 세 건이 섞여있음.
fills_today = [
    {"odno": "1001", "sll_buy_dvsn_cd": "02", "tot_ccld_qty": "10", "avg_prvs": "10000"},  # 매수1 체결
    {"odno": "1002", "sll_buy_dvsn_cd": "01", "tot_ccld_qty": "10", "avg_prvs": "9700"},   # 매도1(손절) 체결
    {"odno": "1003", "sll_buy_dvsn_cd": "02", "tot_ccld_qty": "8",  "avg_prvs": "10200"},  # 매수2 체결
]

r1001 = bot.find_fill_for_odno(fills_today, "1001")
r1002 = bot.find_fill_for_odno(fills_today, "1002")
r1003 = bot.find_fill_for_odno(fills_today, "1003")
r9999 = bot.find_fill_for_odno(fills_today, "9999")  # 존재하지 않는 주문번호

check("ODNO 1001 -> (10주, 10000원) 정확히 매칭", r1001 == (10, 10000.0))
check("ODNO 1002 -> (10주, 9700원) 정확히 매칭 (매수와 안 섞임)", r1002 == (10, 9700.0))
check("ODNO 1003 -> (8주, 10200원) 정확히 매칭", r1003 == (8, 10200.0))
check("존재하지 않는 ODNO -> None", r9999 is None)


# ============================================================
# 4) 매도 실패 시 포지션을 잃지 않고 재시도하는지 (핵심 버그 수정 검증)
# ============================================================
print("\n[4] 매도 주문 실패 시 포지션 보존 + 재시도 테스트")

state = {
    "positions": [{
        "ticker": "005930", "name": "삼성전자",
        "entry_price": 10000, "peak_price": 10000, "qty": 10,
        "entry_date": "2026-09-21",
    }],
    "pending_buys": [],
    "last_signal": {},
}

book_sell = RealtimeBook()
book_sell.data["005930"] = {"price": 9600, "cumulative_volume": 0}  # -4%, 손절 조건(-3%) 충족

# place_order가 실패(rt_cd != "0")를 반환하도록 가짜 함수로 교체
def fake_place_order_fail(*args, **kwargs):
    return {"rt_cd": "1", "msg1": "가상 주문 거부 테스트"}

original_place_order = bot.place_order
original_send_telegram = bot.send_telegram
bot.place_order = fake_place_order_fail
bot.send_telegram = lambda msg: None  # 텔레그램 전송은 테스트에서 생략

bot.check_positions(state, book_sell, "k", "s", "t", "c", "p")

check("매도 주문이 실패해도 포지션이 사라지지 않음", len(state["positions"]) == 1)
check("실패한 포지션에 sell_odno가 안 붙어있음(다음 루프 재시도 가능)",
      "sell_odno" not in state["positions"][0])

# 이번엔 place_order가 성공(rt_cd == "0")하도록 교체
def fake_place_order_success(*args, **kwargs):
    return {"rt_cd": "0", "output": {"ODNO": "5555"}}

bot.place_order = fake_place_order_success
bot.check_positions(state, book_sell, "k", "s", "t", "c", "p")

check("매도 주문 성공 -> sell_odno가 기록됨", state["positions"][0].get("sell_odno") == "5555")

# 이미 sell_odno가 있는 상태에서 다시 check_positions를 불러도 중복 주문 안 나가는지
call_count = {"n": 0}
def fake_place_order_counter(*args, **kwargs):
    call_count["n"] += 1
    return {"rt_cd": "0", "output": {"ODNO": "9999"}}

bot.place_order = fake_place_order_counter
bot.check_positions(state, book_sell, "k", "s", "t", "c", "p")
check("이미 매도 시도 중인 포지션은 중복 주문을 안 냄", call_count["n"] == 0)

bot.place_order = original_place_order
bot.send_telegram = original_send_telegram


# ============================================================
# 5) reconcile: 매도 체결 확인 후 포지션 제거 / 타임아웃 시 되살리기
# ============================================================
print("\n[5] reconcile 테스트 (체결 확인 / 타임아웃 재시도)")

state2 = {
    "positions": [{
        "ticker": "005930", "name": "삼성전자",
        "entry_price": 10000, "peak_price": 10000, "qty": 10,
        "entry_date": "2026-09-21",
        "sell_odno": "7777", "sell_submitted_at": time.time(),
    }],
    "pending_buys": [],
    "last_signal": {},
}

def fake_get_today_fills_filled(*args, **kwargs):
    return [{"odno": "7777", "tot_ccld_qty": "10", "avg_prvs": "9700"}]

original_get_today_fills = bot.get_today_fills
bot.get_today_fills = fake_get_today_fills_filled
bot.send_telegram = lambda msg: None
bot.reconcile(state2, "k", "s", "t", "c", "p")
check("매도 체결 확인되면 포지션이 제거됨", len(state2["positions"]) == 0)

# 타임아웃 시나리오: 체결도 안 됐고 시간도 초과됨 -> 포지션이 되살아나야 함
state3 = {
    "positions": [{
        "ticker": "000660", "name": "SK하이닉스",
        "entry_price": 100000, "peak_price": 100000, "qty": 5,
        "entry_date": "2026-09-21",
        "sell_odno": "8888",
        "sell_submitted_at": time.time() - bot.PENDING_TIMEOUT_SEC - 5,  # 타임아웃 지남
    }],
    "pending_buys": [],
    "last_signal": {},
}

def fake_get_today_fills_empty(*args, **kwargs):
    return []  # 아무 체결도 없음

bot.get_today_fills = fake_get_today_fills_empty
bot.reconcile(state3, "k", "s", "t", "c", "p")
check("타임아웃 지나면 포지션이 되살아남(잃지 않음)", len(state3["positions"]) == 1)
check("되살아난 포지션엔 sell_odno가 제거되어 재시도 가능 상태", "sell_odno" not in state3["positions"][0])

bot.get_today_fills = original_get_today_fills
bot.send_telegram = original_send_telegram


# ============================================================
print(f"\n{'='*40}\n결과: {PASS}개 통과, {FAIL}개 실패\n{'='*40}")
sys.exit(0 if FAIL == 0 else 1)
