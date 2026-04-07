"""한국투자증권 KIS Developers Open API 연동 모듈."""

import json
import time
import logging
import requests
from pathlib import Path
from datetime import datetime
from config_loader import load_config, DATA_DIR

logger = logging.getLogger(__name__)

# ── 기본 설정 ────────────────────────────────────────
BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE = DATA_DIR / "kis_token.json"

# ── 잔고 인메모리 캐시 (5분 TTL) ─────────────────────
_balance_cache: dict = {}
_BALANCE_TTL = 300  # 5분


def _get_cached(key: str):
    entry = _balance_cache.get(key)
    if entry and time.time() - entry["ts"] < _BALANCE_TTL:
        return entry["data"]
    return None


def _set_cached(key: str, data):
    _balance_cache[key] = {"ts": time.time(), "data": data}


def invalidate_balance_cache():
    """잔고 캐시 강제 초기화 (report 호출 시 사용)."""
    _balance_cache.clear()


# ── 토큰 관리 ────────────────────────────────────────

def _get_credentials():
    config = load_config()
    kis = config.get("kis", {})
    app_key    = kis.get("app_key", "")
    app_secret = kis.get("app_secret", "")
    account_no = kis.get("account_no", "")       # 예: "44162659"
    account_cd = kis.get("account_product_cd", "01")  # 예: "01"
    return app_key, app_secret, account_no, account_cd


def _load_cached_token():
    """캐시된 토큰 반환 (만료 시 None)."""
    if TOKEN_CACHE.exists():
        with open(TOKEN_CACHE) as f:
            data = json.load(f)
        # 만료 10분 전부터 갱신
        if data.get("expires_at", 0) - time.time() > 600:
            return data.get("access_token")
    return None


def _save_token(token: str, expires_in: int):
    with open(TOKEN_CACHE, "w") as f:
        json.dump({
            "access_token": token,
            "expires_at": time.time() + expires_in,
        }, f)


def get_token() -> str:
    """액세스 토큰 발급 (캐시 사용)."""
    cached = _load_cached_token()
    if cached:
        return cached

    app_key, app_secret, _, _ = _get_credentials()

    if not app_key or not app_secret:
        raise ValueError("KIS_APP_KEY 또는 KIS_APP_SECRET 환경변수가 설정되지 않았습니다.")

    logger.info(f"KIS 토큰 요청 - app_key: {app_key[:8]}...")

    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }
    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        headers={"content-type": "application/json"},
        json=body,
        timeout=10,
    )

    logger.info(f"KIS 토큰 응답 status={resp.status_code} body={resp.text[:200]}")

    if resp.status_code != 200:
        raise Exception(f"토큰 발급 실패 ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    if "access_token" not in data:
        raise Exception(f"토큰 없음: {data}")

    token = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))
    _save_token(token, expires_in)
    logger.info("KIS 토큰 발급 완료")
    return token


def _headers(tr_id: str) -> dict:
    app_key, app_secret, _, _ = _get_credentials()
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {get_token()}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }


# ── 현재가 조회 ──────────────────────────────────────

def get_kr_price(ticker: str):
    """국내주식 현재가 조회."""
    try:
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=_headers("FHKST01010100"),
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker},
            timeout=8,
        )
        data = resp.json()
        out = data.get("output", {})
        return {
            "ticker": ticker,
            "price": int(out.get("stck_prpr", 0)),
            "change_pct": float(out.get("prdy_ctrt", 0)),
            "volume": int(out.get("acml_vol", 0)),
        }
    except Exception as e:
        logger.error(f"KIS 국내 현재가 오류 {ticker}: {e}")
        return None


def get_us_price(ticker: str, market: str = "NAS"):
    """해외주식 현재가 조회.
    market: NAS(나스닥), NYS(뉴욕), AMS(아멕스)
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/uapi/overseas-price/v1/quotations/price",
            headers=_headers("HHDFS00000300"),
            params={"AUTH": "", "EXCD": market, "SYMB": ticker},
            timeout=8,
        )
        data = resp.json()
        out = data.get("output", {})
        price = float(out.get("last", 0))
        diff  = float(out.get("diff", 0))
        base  = price - diff
        pct   = (diff / base * 100) if base else 0
        return {
            "ticker": ticker,
            "price": price,
            "change_pct": round(pct, 2),
        }
    except Exception as e:
        logger.error(f"KIS 해외 현재가 오류 {ticker}: {e}")
        return None


# ── 계좌 잔고 조회 ───────────────────────────────────

def get_kr_balance() -> str:
    """국내주식 잔고 조회."""
    try:
        app_key, app_secret, account_no, account_cd = _get_credentials()
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=_headers("TTTC8434R"),
            params={
                "CANO": account_no,
                "ACNT_PRDT_CD": account_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        data = resp.json()
        output1 = data.get("output1", [])  # 종목별 잔고
        output2 = data.get("output2", [{}])  # 계좌 총평가

        def safe_int(v):
            try: return int(float(str(v).replace(",", "") or 0))
            except: return 0

        def safe_float(v):
            try: return float(str(v).replace(",", "") or 0)
            except: return 0.0

        lines = ["🇰🇷 <b>국내주식 잔고</b>"]
        total_eval   = safe_int(output2[0].get("scts_evlu_amt", 0)) if output2 else 0
        total_profit = safe_int(output2[0].get("evlu_pfls_smtl_amt", 0)) if output2 else 0
        total_pct    = safe_float(output2[0].get("evlu_erng_rt", 0)) if output2 else 0.0

        for item in output1:
            name      = item.get("prdt_name", "")
            qty       = safe_int(item.get("hldg_qty", 0))
            avg_price = safe_int(item.get("pchs_avg_pric", 0))
            curr      = safe_int(item.get("prpr", 0))
            profit    = safe_int(item.get("evlu_pfls_amt", 0))
            pct       = safe_float(item.get("evlu_pfls_rt", 0))
            if qty == 0:
                continue
            sign = "📈" if profit >= 0 else "📉"
            lines.append(
                f"{sign} {name}: {qty}주 | 현재가 {curr:,}원 | 평단 {avg_price:,}원 | "
                f"손익 {profit:+,}원 ({pct:+.1f}%)"
            )

        lines.append("")
        lines.append(f"국내 총평가: <b>{total_eval:,}원</b>")
        lines.append(f"국내 손익: <b>{total_profit:+,}원 ({total_pct:+.1f}%)</b>")
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"KIS 국내 잔고 오류: {e}")
        return f"국내 잔고 조회 오류: {e}"


def get_us_balance() -> str:
    """해외주식 잔고 조회 (전체 통화)."""
    try:
        app_key, app_secret, account_no, account_cd = _get_credentials()
        resp = requests.get(
            f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
            headers=_headers("TTTS3012R"),
            params={
                "CANO": account_no,
                "ACNT_PRDT_CD": account_cd,
                "WCRC_FRCR_DVSN_CD": "02",   # 02=전체
                "NATN_CD": "840",             # 840=미국
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
            timeout=10,
        )
        data = resp.json()
        output1 = data.get("output1", [])
        output2 = data.get("output2", [{}])

        lines = ["🇺🇸 <b>해외주식 잔고</b>"]
        for item in output1:
            ticker    = item.get("pdno", "")
            qty       = float(item.get("cblc_qty13", 0))
            avg_price = float(item.get("pchs_avg_pric", 0))
            curr      = float(item.get("now_pric2", 0))
            profit    = float(item.get("frcr_evlu_pfls_amt", 0))
            pct       = float(item.get("evlu_pfls_rt", 0))
            if qty == 0:
                continue
            sign = "📈" if profit >= 0 else "📉"
            lines.append(
                f"{sign} {ticker}: {qty}주 | ${curr:.2f} | 평단 ${avg_price:.2f} | "
                f"손익 ${profit:+.2f} ({pct:+.1f}%)"
            )

        if output2:
            total_usd = float(output2[0].get("tot_frcr_cblc_smtl", 0))
            profit_usd = float(output2[0].get("ovrs_tot_pfls", 0))
            lines.append("")
            lines.append(f"해외 총평가: <b>${total_usd:,.2f}</b>")
            lines.append(f"해외 손익: <b>${profit_usd:+,.2f}</b>")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"KIS 해외 잔고 오류: {e}")
        return f"해외 잔고 조회 오류: {e}"


def get_full_balance() -> str:
    """국내 + 해외 잔고 통합 조회."""
    kr = get_kr_balance()
    us = get_us_balance()
    return f"{kr}\n\n{us}"


def get_kr_balance_raw() -> dict:
    """국내주식 잔고 raw 데이터 반환 (5분 캐시)."""
    cached = _get_cached("kr_raw")
    if cached is not None:
        return cached
    try:
        app_key, app_secret, account_no, account_cd = _get_credentials()
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=_headers("TTTC8434R"),
            params={
                "CANO": account_no, "ACNT_PRDT_CD": account_cd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        data = resp.json()
        # API 오류 응답 체크
        if data.get("rt_cd") not in ("0", None) and data.get("rt_cd") != 0:
            logger.error(f"KIS 국내 잔고 API 오류: {data.get('msg1', '')} (rt_cd={data.get('rt_cd')})")
            return {"holdings": [], "total": {}}

        output1 = data.get("output1", [])
        output2 = data.get("output2", [{}])

        def safe_int(v):
            try: return int(float(str(v).replace(",", "") or 0))
            except: return 0

        def safe_float(v):
            try: return float(str(v).replace(",", "") or 0)
            except: return 0.0

        holdings = []
        for item in output1:
            qty = safe_int(item.get("hldg_qty", 0))
            if qty == 0:
                continue
            holdings.append({
                "ticker": item.get("pdno", ""),
                "name": item.get("prdt_name", ""),
                "qty": qty,
                "avg_price": safe_int(item.get("pchs_avg_pric", 0)),
                "curr_price": safe_int(item.get("prpr", 0)),
                "profit": safe_int(item.get("evlu_pfls_amt", 0)),
                "profit_pct": safe_float(item.get("evlu_pfls_rt", 0)),
                "eval_amt": safe_int(item.get("evlu_amt", 0)),
                "invested": safe_int(item.get("pchs_amt", 0)),
            })

        total = {}
        if output2:
            eval_amt  = safe_int(output2[0].get("scts_evlu_amt", 0))
            profit    = safe_int(output2[0].get("evlu_pfls_smtl_amt", 0))
            invested  = safe_int(output2[0].get("pchs_amt_smtl_amt", 0))
            # evlu_erng_rt가 0이면 직접 계산
            pct = safe_float(output2[0].get("evlu_erng_rt", 0))
            if pct == 0 and invested:
                pct = round(profit / invested * 100, 2)
            total = {
                "eval_amt":   eval_amt,
                "profit":     profit,
                "profit_pct": pct,
                "invested":   invested,
            }
        result = {"holdings": holdings, "total": total}
        _set_cached("kr_raw", result)
        return result
    except Exception as e:
        logger.error(f"KIS 국내 raw 잔고 오류: {e}")
        return {"holdings": [], "total": {}}


def get_us_balance_raw() -> dict:
    """해외주식 잔고 raw 데이터 반환 (5분 캐시)."""
    cached = _get_cached("us_raw")
    if cached is not None:
        return cached
    try:
        app_key, app_secret, account_no, account_cd = _get_credentials()
        resp = requests.get(
            f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
            headers=_headers("TTTS3012R"),
            params={
                "CANO": account_no, "ACNT_PRDT_CD": account_cd,
                "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                "TR_MKET_CD": "00", "INQR_DVSN_CD": "00",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") not in ("0", None) and data.get("rt_cd") != 0:
            logger.error(f"KIS 해외 잔고 API 오류: {data.get('msg1', '')} (rt_cd={data.get('rt_cd')})")
            return {"holdings": [], "total": {}}

        output1 = data.get("output1", [])
        output2 = data.get("output2", [{}])

        def safe_float(v):
            try: return float(str(v).replace(",", "") or 0)
            except: return 0.0

        holdings = []
        for item in output1:
            qty = safe_float(item.get("cblc_qty13", 0))
            if qty == 0:
                continue
            avg  = safe_float(item.get("pchs_avg_pric", 0))
            curr = safe_float(item.get("now_pric2", 0))
            profit = safe_float(item.get("frcr_evlu_pfls_amt", 0))
            invested = avg * qty
            pct = (profit / invested * 100) if invested else 0
            holdings.append({
                "ticker": item.get("pdno", ""),
                "name": item.get("prdt_name", item.get("pdno", "")),
                "qty": qty,
                "avg_price": avg,
                "curr_price": curr,
                "profit": profit,
                "profit_pct": round(pct, 2),
                "eval_amt": curr * qty,
                "invested": invested,
            })

        total = {}
        if output2:
            invested_us = sum(h["invested"] for h in holdings)
            profit_us = safe_float(output2[0].get("ovrs_tot_pfls", 0))
            pct_us = round(profit_us / invested_us * 100, 2) if invested_us else 0
            total = {
                "eval_amt": safe_float(output2[0].get("tot_frcr_cblc_smtl", 0)),
                "profit": profit_us,
                "profit_pct": pct_us,
                "invested": invested_us,
            }
        result = {"holdings": holdings, "total": total}
        _set_cached("us_raw", result)
        return result
    except Exception as e:
        logger.error(f"KIS 해외 raw 잔고 오류: {e}")
        return {"holdings": [], "total": {}}


# ── 주문 실행 ────────────────────────────────────────

# 시장 코드 매핑 (해외)
_MARKET_MAP = {
    "NAS": ("TTTT1002U", "TTTT1006U"),  # 나스닥 매수/매도
    "NYS": ("TTTT1002U", "TTTT1006U"),  # 뉴욕
    "AMS": ("TTTT1002U", "TTTT1006U"),  # 아멕스
}


def place_kr_order(ticker: str, side: str, qty: int, price: int = 0) -> str:
    """국내주식 주문.
    side: 'buy' or 'sell'
    price: 0이면 시장가
    """
    try:
        app_key, app_secret, account_no, account_cd = _get_credentials()
        tr_id = "TTTC0802U" if side == "buy" else "TTTC0801U"
        ord_dvsn = "01" if price == 0 else "00"  # 01=시장가, 00=지정가

        body = {
            "CANO": account_no,
            "ACNT_PRDT_CD": account_cd,
            "PDNO": ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        resp = requests.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=_headers(tr_id),
            json=body,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") == "0":
            out = data.get("output", {})
            return (
                f"✅ 국내주식 {'매수' if side == 'buy' else '매도'} 주문 완료\n"
                f"종목: {ticker} | 수량: {qty}주 | "
                f"{'시장가' if price == 0 else f'{price:,}원'}\n"
                f"주문번호: {out.get('ODNO', 'N/A')}"
            )
        else:
            return f"❌ 주문 실패: {data.get('msg1', '알 수 없는 오류')}"
    except Exception as e:
        return f"국내 주문 오류: {e}"


def place_us_order(ticker: str, side: str, qty: int, price: float = 0,
                   market: str = "NAS") -> str:
    """해외주식 주문.
    side: 'buy' or 'sell'
    price: 0이면 시장가
    market: NAS, NYS, AMS
    """
    try:
        app_key, app_secret, account_no, account_cd = _get_credentials()
        buy_tr, sell_tr = _MARKET_MAP.get(market, ("TTTT1002U", "TTTT1006U"))
        tr_id = buy_tr if side == "buy" else sell_tr
        ord_dvsn = "32" if price == 0 else "00"  # 32=시장가, 00=지정가

        body = {
            "CANO": account_no,
            "ACNT_PRDT_CD": account_cd,
            "OVRS_EXCG_CD": market,
            "PDNO": ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
        }
        resp = requests.post(
            f"{BASE_URL}/uapi/overseas-stock/v1/trading/order",
            headers=_headers(tr_id),
            json=body,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") == "0":
            out = data.get("output", {})
            price_str = "시장가" if price == 0 else f"${price:.2f}"
            return (
                f"✅ 해외주식 {'매수' if side == 'buy' else '매도'} 주문 완료\n"
                f"종목: {ticker} | 수량: {qty}주 | {price_str}\n"
                f"주문번호: {out.get('ODNO', 'N/A')}"
            )
        else:
            return f"❌ 주문 실패: {data.get('msg1', '알 수 없는 오류')}"
    except Exception as e:
        return f"해외 주문 오류: {e}"


# ── 편의 함수 ─────────────────────────────────────────

def is_configured() -> bool:
    """KIS API 설정이 있는지 확인."""
    app_key, app_secret, account_no, _ = _get_credentials()
    return bool(app_key and app_secret and account_no)
