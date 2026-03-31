# 주식 알림 텔레그램 봇

매일 보유 종목 현황, 시황, 뉴스를 텔레그램으로 알려주는 개인 주식 어시스턴트 봇.

## 기능

- **매일 오전 8시** 포트폴리오 리포트 (수익률, 시황, 환율)
- **매일 오후 9시 30분** 미장 개장 전 브리핑 (선물, 오버나이트 뉴스)
- **30분마다** 보유 종목 중요 뉴스 모니터링 → 이슈 발생 시 즉시 알림
- **자연어 질문** 대화 (실시간 웹 검색 포함, 대화 맥락 유지)
- **매수/매도 명령어**로 포트폴리오 업데이트

## 텔레그램 명령어

| 명령어 | 설명 |
|--------|------|
| `/buy AAPL 10 150.5` | 매수 기록 |
| `/buy 005930 10 70000 삼성전자` | 한국 주식 매수 |
| `/sell AAPL 5` | 매도 기록 |
| `/portfolio` | 현재 보유종목 확인 |
| `/report` | 즉시 리포트 |
| `/reset` | 대화 기록 초기화 |
| 자연어 | 뭐든 물어보세요 |

## 설치

### 1. 의존성 설치

```bash
pip install yfinance pykrx anthropic duckduckgo-search requests
```

### 2. 설정 파일 생성

```bash
cp config.example.json config.json
```

`config.json`을 열고 아래 항목을 채우세요:
- `anthropic_api_key`: [Anthropic Console](https://console.anthropic.com)에서 발급
- `telegram.bot_token`: BotFather에서 생성
- `telegram.chat_id`: `@userinfobot`으로 확인
- `portfolio`: 보유 종목 입력

### 3. 텔레그램 봇 만들기

1. 텔레그램에서 `@BotFather` → `/newbot`
2. 봇 이름 및 username 설정
3. 발급된 토큰을 `config.json`에 입력

### 4. 스케줄러 등록 (macOS)

```bash
# 매일 오전 8시 리포트
cp launchd/com.stockalert.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockalert.daily.plist

# 매일 오후 9시 30분 미장 브리핑
cp launchd/com.stockalert.premarket.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockalert.premarket.plist

# 30분마다 뉴스 모니터링
cp launchd/com.stockalert.newsmonitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockalert.newsmonitor.plist

# 텔레그램 봇 (항상 실행)
cp launchd/com.stockalert.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.stockalert.bot.plist
```

### 5. 수동 테스트

```bash
python stock_alert.py      # 리포트 즉시 발송
python premarket_alert.py  # 미장 브리핑 즉시 발송
python news_monitor.py     # 뉴스 모니터링 1회 실행
python bot.py              # 봇 실행
```

## 파일 구조

```
stock_alert_bot/
├── bot.py              # 텔레그램 봇 (자연어 + 명령어)
├── stock_alert.py      # 일일 포트폴리오 리포트
├── premarket_alert.py  # 미장 개장 전 브리핑
├── news_monitor.py     # 뉴스 모니터링
├── config.json         # 설정 (gitignore됨)
├── config.example.json # 설정 템플릿
└── launchd/            # macOS 스케줄러 설정
```
