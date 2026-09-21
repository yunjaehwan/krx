# KRX 실시간 추격 자동매매 (정규장 전용, 제가 검토·수정·테스트한 버전)

## GPT가 만든 버전과 비교해서 무엇을 고쳤나

GPT의 V2/V3 코드를 검토하면서 실제로 위험할 수 있는 문제 두 가지를 발견해서 고쳤습니다.

1. **체결 확인이 종목 단위였던 문제**
   기존 코드는 "이 종목 오늘 체결 전부"를 가져와서 판단해서, 같은 종목을 하루
   두 번 이상 거래하면(매수→손절→재매수 등) 어떤 주문의 체결인지 헷갈릴 수
   있었습니다. 이 버전은 주문 낼 때 받는 **주문번호(ODNO)로 정확히 매칭**합니다.

2. **매도 주문이 실패하면 포지션을 잃어버릴 수 있었던 문제**
   기존 코드는 매도 주문을 "넣는 순간" 바로 청산 처리하고 내부 기록에서
   지워버렸습니다. 만약 그 주문이 거부되면, 계좌엔 주식이 남아있는데 봇은
   더 이상 감시를 안 하는 상태가 될 수 있었습니다. 이 버전은 **실제 체결이
   확인될 때까지 포지션을 계속 들고 있고**, 60초 안에 체결이 안 되면
   자동으로 다시 매도를 시도합니다.

그리고 **애프터마켓(8~20시) 확장은 제외**하고 정규장(09:00~15:30)만
다룹니다. 애프터마켓은 주문 방식과 유동성이 달라서, 지금 조건을 그대로
확장하면 GPT 버전과 같은 위험(장마감 강제매도가 애프터마켓 규칙 때문에
실패하는데 포지션 추적을 놓치는 상황)이 재현될 수 있어서입니다.

## 테스트

`test_logic.py`로 핵심 로직 19개를 실제 KIS 서버 없이 가짜 데이터로
검증했습니다 (전부 통과). 신호 판단, 거래량/가격 윈도 계산, 주문번호
매칭, 매도 실패 시 포지션 보존과 재시도까지 확인했습니다.

로컬에서 재실행:
```
python3 test_logic.py
```

**중요**: 이건 로직(코드가 의도한 대로 도는지)만 검증한 거지, 실제
한국투자증권 서버와의 통신(웹소켓 필드 포맷, 주문 응답 필드명 등)은
아직 실전(모의투자) 환경에서 처음 돌려봐야 확인됩니다. 지금까지 이
대화에서 KIS API 연동할 때마다 그랬듯, 처음 돌리면 필드명이나 파라미터
하나 정도는 안 맞을 가능성이 있습니다 — 그러면 로그 캡처해서 보여주세요.

## 여전히 검증 안 된 전략입니다

분봉 데이터로 과거 백테스트를 할 수 없는 실험적 전략입니다. 모의투자로
충분히 지켜본 뒤 판단하세요.

## 설치 (VPS)

```
git clone https://github.com/yunjaehwan/krx.git /tmp/krx-install
cd /tmp/krx-install
sudo bash deploy/install.sh
sudo nano /opt/krx-bot/.env   # KIS_APP_KEY 등 입력
sudo systemctl start krx-bot.service
sudo systemctl status krx-bot.service
sudo tail -f /opt/krx-bot/logs/bot.log
```

## 파일 구성

- `krx_realtime_ws.py` — 웹소켓 실시간 체결 수신 (KRX+NXT 통합, H0UNCNT0)
- `krx_realtime_autotrader.py` — 전략 로직 + 주문 (정규장 전용)
- `test_logic.py` — 핵심 로직 테스트
- `deploy/install.sh` — 서버 최초 설치
- `deploy/run_bot.sh` — 정규장 시간에만 봇 실행하는 래퍼
- `deploy/krx-bot.service` — systemd 서비스 정의
- `requirements.txt`, `env.example`, `.gitignore`

## 긴급 중지

```
sudo systemctl stop krx-bot.service
```
