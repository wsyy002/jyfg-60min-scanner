#!/usr/bin/env python3
"""
60分钟K线扫描器 v3.0 — JYFG波段引擎版
GitHub Actions 适配版

数据源: ① Sina(腾讯行情) → ② akshare(东方财富, 境外慢)
模式:
  - scan (默认): 仅扫描+飞书推送，GitHub云端Runner可用
  - full: 完整模式(含同花顺下单)，需Self-Hosted Runner

运行:
  python3 60min_scanner_ga.py
  JYFG_MODE=full python3 60min_scanner_ga.py  # 下单模式(需本地环境)
"""

import os, sys, json, time
import requests
import numpy as np
from datetime import datetime, timedelta, timezone

BJT = timezone(timedelta(hours=8))
now = datetime.now(BJT)

# ─── 运行模式 ──
MODE = os.environ.get("JYFG_MODE", "scan")
IS_CLOUD = os.environ.get("GITHUB_ACTIONS", "") == "true"
IS_FULL = MODE == "full" and not IS_CLOUD

# ─── 路径配置 ──
LOG_DIR = os.environ.get(
    "JYFG_LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
)
os.makedirs(LOG_DIR, exist_ok=True)
# 股票缓存：优先用仓库自带的（GA环境），否则用日志目录
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STOCK_LIST_CACHE = (
    os.path.join(_SCRIPT_DIR, 'stock_list_cache.json')  # 仓库自带的缓存
    if os.path.exists(os.path.join(_SCRIPT_DIR, 'stock_list_cache.json'))
    else os.path.join(LOG_DIR, 'stock_list_cache.json')  # 运行时生成的缓存
)

# ─── 同花顺模拟交易配置（仅full模式） ──
ACCOUNT = {
    'usrid': '115841599', 'yybid': '997376',
    'shareholder_sh': 'A542347955', 'shareholder_sz': '00166627612',
}
SIM_SCRIPTS = os.environ.get(
    "SIM_SCRIPTS",
    "/home/guowu/.openclaw/workspace/skills/模拟炒股/─ú─Γ│┤╣╔/scripts"
)

# ─── 配置参数 ──────────────────────────
CONFIG = {
    'short_ma': 5,
    'mid_ma': 10,
    'trend_ma': 20,
    'long_ma': 60,
    'volume_factor': 1.30,
    'double_vol_factor': 2.0,
    'shrink_factor': 0.75,
    'stop_loss_atr_mult': 3.0,
    'stop_loss_period': 20,
    'signal_interval': 10,
    'base_buy_ratio': 0.10,
}

# 最大持仓（从环境变量或默认）
MAX_HOLDINGS = int(os.environ.get("JYFG_MAX_HOLDINGS", "6"))


def safe_parse_turnover(rv, d=0):
    try:
        v = float(rv)
        return d if v < 0 or v > 50 else round(v, 2)
    except:
        return d


def ema(arr, p):
    r = [arr[0]]
    a = 2.0 / (p + 1)
    for i in range(1, len(arr)):
        r.append(a * arr[i] + (1 - a) * r[-1])
    return r


def sma(arr, n, m=1):
    if len(arr) == 0:
        return []
    r = [arr[0]]
    for i in range(1, len(arr)):
        r.append((r[-1] * (n - m) + arr[i] * m) / n)
    return r


def log(msg):
    print(f"[{now.strftime('%H:%M')}] {msg}")


# ─── 数据源 ────────────────────────────────

def load_stock_list():
    """加载股票列表：优先从仓库缓存读取，akshare为备选"""
    if os.path.exists(STOCK_LIST_CACHE):
        try:
            with open(STOCK_LIST_CACHE, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log(f"读取缓存失败: {e}")

    # 备选：akshare
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot()
        ci = []
        for _, r in df.iterrows():
            rc, n = str(r['代码']), str(r['名称'])
            c = rc[2:] if rc.startswith(('sh', 'sz')) else rc
            if rc.startswith('bj') or n.startswith(('*ST', 'ST', '退')):
                continue
            if c.startswith(('688', '689', '8', '4', '9')):
                continue
            ci.append({'code': c, 'name': n})
        # 写入缓存
        os.makedirs(os.path.dirname(STOCK_LIST_CACHE), exist_ok=True)
        with open(STOCK_LIST_CACHE, 'w', encoding='utf-8') as f:
            json.dump(ci, f, ensure_ascii=False, indent=2)
        return ci
    except Exception as e:
        log(f"akshare spot 失败: {e}")
        return []


def enrich_tencent(stocks):
    """从腾讯行情补充价格/涨跌幅/换手率"""
    if not stocks:
        return stocks
    cm = {s['code']: s for s in stocks}
    blks = []
    cur = []
    for s in stocks:
        p = 'sh' if s['code'].startswith('6') else 'sz'
        cur.append(f"{p}{s['code']}")
        if len(cur) >= 120:
            blks.append(cur)
            cur = []
    if cur:
        blks.append(cur)
    for blk in blks:
        try:
            r = requests.get(
                f"http://qt.gtimg.cn/q={','.join(blk)}",
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=10,
            )
            for l in r.text.strip().split('\n'):
                if '~' not in l:
                    continue
                f = l.split('~')
                tc = f[2]
                if tc not in cm:
                    continue
                s = cm[tc]
                try:
                    p = float(f[3]) if f[3] else 0
                    lc = float(f[4]) if f[4] else 0
                    s.update({
                        'price': round(p, 2),
                        'change_pct': round((p - lc) / lc * 100, 2) if lc > 0 else 0,
                        'turnover': safe_parse_turnover(f[38] if len(f) > 38 else 0),
                    })
                except:
                    pass
        except:
            pass
        time.sleep(0.03)
    return stocks


def get_all_stocks():
    """获取全市场股票列表（含实时行情）"""
    ci = load_stock_list()
    if not ci:
        return []
    stocks = [
        {'code': c['code'], 'name': c['name'],
         'price': 0, 'change_pct': 0, 'turnover': 0}
        for c in ci
    ]
    return [
        s for s in enrich_tencent(stocks)
        if s['price'] > 0 and s['price'] <= 200
    ]


def get_60min_kline(code):
    """
    获取60分钟K线数据
    ① Sina（快，境外友好） → ② akshare（东方财富，慢）
    """
    # ── ① Sina 主数据源（快） ──
    p = 'sh' if code.startswith('6') else 'sz'
    try:
        r = requests.get(
            f"https://quotes.sina.com.cn/cn/api/jsonp_v2.php/var%20_{code}"
            f"/CN_MarketData.getKLineData?symbol={p}{code}&scale=60&ma=no&datalen=80",
            timeout=10,
        )
        t = r.text
        if '(' in t:
            d = json.loads(t[t.find('(') + 1:t.rfind(')')])
            if d and len(d) >= 60:
                return [
                    {
                        'time': b.get('day', b.get('date', '')),
                        'open': float(b['open']),
                        'high': float(b['high']),
                        'low': float(b['low']),
                        'close': float(b['close']),
                        'volume': float(b.get('volume', 0)),
                    }
                    for b in d
                ]
    except:
        pass

    # ── ② akshare 备选 ──
    try:
        import akshare as ak
        start = (now - timedelta(days=40)).strftime('%Y-%m-%d')
        end = now.strftime('%Y-%m-%d')
        df = ak.stock_zh_a_hist_min_em(
            symbol=code, period="60", start_date=start, end_date=end, adjust=""
        )
        if df is not None and len(df) >= 60:
            result = []
            for _, row in df.iterrows():
                result.append({
                    'time': row['时间'],
                    'open': float(row['开盘']),
                    'high': float(row['最高']),
                    'low': float(row['最低']),
                    'close': float(row['收盘']),
                    'volume': float(row['成交量']),
                })
            return result
    except Exception:
        pass

    return None


def check_listing(code):
    """检查是否上市不足180天（次新股排除）"""
    p = 'sh' if code.startswith('6') else 'sz'
    try:
        r = requests.get(
            f"https://quotes.sina.com.cn/cn/api/jsonp_v2.php/var%20_d{code}"
            f"/CN_MarketData.getKLineData?symbol={p}{code}&scale=240&ma=no&datalen=300",
            timeout=8,
        )
        t = r.text
        if '(' not in t:
            return True
        d = json.loads(t[t.find('(') + 1:t.rfind(')')])
        if d:
            fd = d[0].get('day', d[0].get('date', ''))[:10]
            if fd:
                dt_obj = datetime.strptime(fd, '%Y-%m-%d')
                if (now - dt_obj).days < 180:
                    return False
    except:
        pass
    return True


# ─── 完整模式：同花顺交易（仅Self-Hosted Runner） ──

def _load_trading_modules():
    """加载同花顺模拟交易模块"""
    if not IS_FULL:
        return None, None
    try:
        sys.path.insert(0, SIM_SCRIPTS)
        from stock_query import StockQueryService
        from stock_trading import StockTradingService
        return StockQueryService(), StockTradingService()
    except Exception as e:
        log(f"⚠️ 加载同花顺模块失败(非full模式可忽略): {e}")
        return None, None


def get_account_info(qsvc):
    if qsvc is None:
        return None
    try:
        fund = qsvc.query_fund(usrid=ACCOUNT['usrid'])
        return {
            'total_asset': float(fund.get('zjye', 0)),
            'available': float(fund.get('kyje', 0)),
            'frozen': float(fund.get('djje', 0)),
        }
    except Exception as e:
        log(f"查资金失败: {e}")
        return None


def get_positions(qsvc):
    if qsvc is None:
        return []
    try:
        result = qsvc.query_positions(usrid=ACCOUNT['usrid'])
        positions = (
            result.get('positions', [])
            if isinstance(result, dict)
            else (result if isinstance(result, list) else [])
        )
        return [
            {
                'code': p.get('zqdm', ''),
                'name': p.get('zqmc', ''),
                'shares': int(p.get('gpsl', 0)) + int(p.get('djsl', 0)),
                'cost': float(p.get('gpcb', 0)),
                'market_val': float(p.get('gpsz', 0)),
            }
            for p in positions
            if int(p.get('gpsl', 0)) + int(p.get('djsl', 0)) > 0
        ]
    except Exception as e:
        log(f"查持仓失败: {e}")
        return []


def place_buy_order(tsvc, code, price, quantity):
    if tsvc is None:
        return {'success': False, 'error': '下单模块未加载'}
    try:
        market = '2' if code.startswith('6') else '1'
        sh = (
            ACCOUNT['shareholder_sh']
            if market == '2'
            else ACCOUNT['shareholder_sz']
        )
        result = tsvc.place_order(
            usrid=ACCOUNT['usrid'],
            stock_code=code,
            shareholder_account=sh,
            market_code=market,
            price=price,
            quantity=quantity,
            direction='B',
            yybid=ACCOUNT['yybid'],
        )
        return result
    except Exception as e:
        return {'success': False, 'error': str(e)}


def place_sell_order(tsvc, code, price, quantity):
    if tsvc is None:
        return {'success': False, 'error': '下单模块未加载'}
    try:
        market = '2' if code.startswith('6') else '1'
        sh = (
            ACCOUNT['shareholder_sh']
            if market == '2'
            else ACCOUNT['shareholder_sz']
        )
        result = tsvc.place_order(
            usrid=ACCOUNT['usrid'],
            stock_code=code,
            shareholder_account=sh,
            market_code=market,
            price=price,
            quantity=quantity,
            direction='S',
            yybid=ACCOUNT['yybid'],
        )
        return result
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ─── JYFG波段引擎 v3.0 ──────────────────────────

def compute_jyfg_indicators(klines):
    """
    计算JYFG波段公式全部指标
    返回字典包含所有技术指标和趋势评分
    """
    n = len(klines)
    closes = np.array([k['close'] for k in klines], dtype=float)
    highs = np.array([k['high'] for k in klines], dtype=float)
    lows = np.array([k['low'] for k in klines], dtype=float)
    opens = np.array([k['open'] for k in klines], dtype=float)

    # ── EMA趋势系统 ──
    e8 = np.array(ema(closes.tolist(), 8))
    e13 = np.array(ema(closes.tolist(), 13))
    e21 = np.array(ema(closes.tolist(), 21))
    e34 = np.array(ema(closes.tolist(), 34))
    e55 = np.array(ema(closes.tolist(), 55))
    e89 = np.array(ema(closes.tolist(), 89))

    # ── MACD ──
    diff_arr = np.array(ema(closes.tolist(), 12)) - np.array(
        ema(closes.tolist(), 26)
    )
    dea_arr = np.array(ema(diff_arr.tolist(), 9))
    macd_val_arr = (diff_arr - dea_arr) * 2

    # ── KDJ ──
    rsv_arr = np.zeros(n)
    for i in range(n):
        hh = np.max(highs[max(0, i - 8):i + 1])
        ll = np.min(lows[max(0, i - 8):i + 1])
        rsv_arr[i] = (
            50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        )
    k_arr = np.array(sma(rsv_arr.tolist(), 3))
    d_arr = np.array(sma(k_arr.tolist(), 3))
    j_arr = 3 * k_arr - 2 * d_arr

    # ── RSI6 ──
    rsi_arr = np.ones(n) * 50
    avg_up, avg_down = 0, 0
    for i in range(1, n):
        up = max(closes[i] - closes[i - 1], 0)
        down = abs(min(closes[i] - closes[i - 1], 0))
        if i == 1:
            avg_up, avg_down = up, down
        else:
            avg_up = (avg_up * 5 + up) / 6
            avg_down = (avg_down * 5 + down) / 6
        rsi_arr[i] = (
            100 - 100 / (1 + avg_up / max(avg_down, 0.001))
            if avg_down > 0
            else 100
        )

    # ── ATR14 ──
    tr_arr = np.zeros(n)
    for i in range(1, n):
        tr_arr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    atr14_arr = np.array(sma(tr_arr.tolist(), 14))

    # ── 成交量 ──
    vol_arr = np.array([k.get('volume', 0) for k in klines], dtype=float)
    vol5_arr = (
        np.convolve(vol_arr, np.ones(5) / 5, mode='same') if n >= 5 else vol_arr
    )

    # ── 高低点 ──
    hh20_arr = np.array(
        [np.max(highs[max(0, i - 19):i + 1]) for i in range(n)]
    )
    ll20_arr = np.array(
        [np.min(lows[max(0, i - 19):i + 1]) for i in range(n)]
    )
    hh60_arr = np.array(
        [np.max(highs[max(0, i - 59):i + 1]) for i in range(n)]
    )
    ll60_arr = np.array(
        [np.min(lows[max(0, i - 59):i + 1]) for i in range(n)]
    )

    # ── 最新值 ──
    i = n - 1
    vol_ma5 = vol5_arr[i]
    is_fangliang = (
        vol_arr[i] > vol_ma5 * CONFIG['volume_factor'] if vol_ma5 > 0 else False
    )
    is_beiliang = (
        vol_arr[i] > vol_ma5 * CONFIG['double_vol_factor'] if vol_ma5 > 0 else False
    )
    is_suoliang = (
        vol_arr[i] < vol_ma5 * CONFIG['shrink_factor'] if vol_ma5 > 0 else False
    )

    is_yang = closes[i] >= opens[i]
    shiti = abs(closes[i] - opens[i])
    shangying = highs[i] - max(closes[i], opens[i])
    xiaying = min(closes[i], opens[i]) - lows[i]

    zhangfu = (
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        if i > 0 and closes[i - 1] > 0
        else 0
    )

    # ── 趋势判断 ──
    shangzhang_trend = (
        e13[i] > e34[i] and e34[i] > e55[i] and e55[i] > e89[i]
    )
    xiajiang_trend = (
        e13[i] < e34[i] and e34[i] < e55[i] and e55[i] < e89[i]
    )
    zhendang_trend = not shangzhang_trend and not xiajiang_trend

    ema13_up = e13[i] > e13[i - 1] if i > 0 else True
    ema34_up = e34[i] > e34[i - 1] if i > 0 else True

    # ── 趋势评分 (0-100) ──
    score = 0
    if shangzhang_trend:
        score += 30
    if e13[i] > e34[i]:
        score += 20
    if diff_arr[i] > dea_arr[i]:
        score += 20
    if closes[i] > e13[i]:
        score += 10
    if is_fangliang:
        score += 10
    if rsi_arr[i] > 50:
        score += 10

    # ── 信号间隔检查 ──
    si = CONFIG['signal_interval']
    recent_trend_start = False
    recent_huicai = False
    recent_break = False
    recent_kdj_jc = False
    for j in range(max(1, i - si), i):
        if e13[j - 1] <= e34[j - 1] and e13[j] > e34[j]:
            recent_trend_start = True
        if (
            lows[j] <= e13[j]
            and closes[j] > e13[j]
            and closes[j - 1] < e13[j - 1]
        ):
            recent_huicai = True
        if closes[j] > hh20_arr[j - 1]:
            recent_break = True
        if k_arr[j - 1] <= d_arr[j - 1] and k_arr[j] > d_arr[j]:
            recent_kdj_jc = True

    # ── 买入信号 ──
    trend_start = (
        (e13[i - 1] <= e34[i - 1] and e13[i] > e34[i]) if i > 0 else False
    )
    trend_start_buy = (
        trend_start
        and diff_arr[i] > dea_arr[i]
        and closes[i] > e13[i]
        and is_fangliang
        and not recent_trend_start
    )

    huicai_buy = (
        (lows[i] <= e13[i] and closes[i] > e13[i] and closes[i - 1] < e13[i - 1])
        if i > 0
        else False
    )
    huicai_buy = huicai_buy and not recent_huicai

    break_buy = (
        (closes[i] > hh20_arr[i - 1] if i > 0 else False)
        and is_fangliang
        and closes[i] > e13[i]
    )
    break_buy = break_buy and not recent_break

    kdj_jc = (
        (k_arr[i - 1] <= d_arr[i - 1] and k_arr[i] > d_arr[i]) if i > 0 else False
    )
    kdj_jc = kdj_jc and not recent_kdj_jc

    # ── 卖出信号 ──
    phase_high = np.max(
        highs[max(0, i - CONFIG['stop_loss_period'] + 1):i + 1]
    )
    dynamic_stop = phase_high - atr14_arr[i] * CONFIG['stop_loss_atr_mult']

    if shangzhang_trend:
        stop_price = dynamic_stop
    else:
        stop_price = e34[i]

    stop_loss_signal = closes[i] < stop_price
    trend_broken = (
        closes[i] < e34[i] and e13[i] < e13[i - 1] if i > 0 else False
    )
    macd_dead = (
        (diff_arr[i - 1] >= dea_arr[i - 1] and diff_arr[i] < dea_arr[i])
        if i > 0
        else False
    )
    momentum_fade = macd_dead and closes[i] < e13[i]
    high_profit = (
        highs[i] >= hh20_arr[i - 1]
        and rsi_arr[i] > 80
        and diff_arr[i] < diff_arr[i - 1]
    ) if i > 0 else False
    kdj_sc = (
        (k_arr[i - 1] >= d_arr[i - 1] and k_arr[i] < d_arr[i])
        if i > 0
        else False
    )

    high_risk = rsi_arr[i] > 80 and (diff_arr[i] < diff_arr[i - 1] if i > 0 else False)

    # ── 信号等级 ──
    buy_grade = 0
    if break_buy:
        buy_grade = max(buy_grade, 3)
    if trend_start_buy:
        buy_grade = max(buy_grade, 2)
    if huicai_buy:
        buy_grade = max(buy_grade, 1)
    if kdj_jc and score >= 40:
        buy_grade = max(buy_grade, 1)

    sell_grade = 0
    if stop_loss_signal:
        sell_grade = max(sell_grade, 3)
    if high_profit:
        sell_grade = max(sell_grade, 2)
    if trend_broken:
        sell_grade = max(sell_grade, 1)
    if momentum_fade:
        sell_grade = max(sell_grade, 1)

    has_buy_signal = buy_grade > 0 and score >= 40
    has_sell_signal = sell_grade > 0

    return {
        'close': closes[i],
        'high': highs[i],
        'low': lows[i],
        'open': opens[i],
        'ema13': e13[i],
        'ema34': e34[i],
        'ema55': e55[i],
        'ema89': e89[i],
        'ema13_up': ema13_up,
        'ema34_up': ema34_up,
        'diff': diff_arr[i],
        'dea': dea_arr[i],
        'macd_value': macd_val_arr[i],
        'diff_up': diff_arr[i] > diff_arr[i - 1] if i > 0 else True,
        'k': k_arr[i],
        'd': d_arr[i],
        'j': j_arr[i],
        'rsi6': rsi_arr[i],
        'atr14': atr14_arr[i],
        'stop_price': stop_price,
        'phase_high': phase_high,
        'vol_ratio': vol_arr[i] / vol_ma5 if vol_ma5 > 0 else 1,
        'is_fangliang': is_fangliang,
        'is_beiliang': is_beiliang,
        'is_suoliang': is_suoliang,
        'is_yang': is_yang,
        'shiti': shiti,
        'shangying': shangying,
        'xiaying': xiaying,
        'zhangfu': zhangfu,
        'uptrend': shangzhang_trend,
        'downtrend': xiajiang_trend,
        'sideways': zhendang_trend,
        'score': score,
        'buy_grade': buy_grade,
        'sell_grade': sell_grade,
        'has_buy': has_buy_signal,
        'has_sell': has_sell_signal,
        'stop_loss': stop_loss_signal,
        'trend_broken': trend_broken,
        'momentum_fade': momentum_fade,
        'high_profit': high_profit,
        'high_risk': high_risk,
        'buy_type': (
            '突破买'
            if break_buy
            else '趋势启动'
            if trend_start_buy
            else '回踩买'
            if huicai_buy
            else 'KDJ金叉'
            if kdj_jc
            else ''
        ),
        'sell_type': (
            '止损'
            if stop_loss_signal
            else '高位止盈'
            if high_profit
            else '趋势破坏'
            if trend_broken
            else '动能衰减'
            if momentum_fade
            else 'KDJ死叉'
            if kdj_sc
            else ''
        ),
        'time': klines[i]['time'],
    }


def get_buy_ratio(ind):
    if not ind:
        return CONFIG['base_buy_ratio']
    grade = ind['buy_grade']
    score = ind['score']
    if grade >= 3:
        return 0.15
    elif grade >= 2:
        base = 0.12
    else:
        base = 0.08
    if score >= 80:
        return base * 1.2
    elif score >= 60:
        return base
    elif score >= 40:
        return base * 0.8
    else:
        return base * 0.6


def push_feishu(msg):
    """推送飞书卡片"""
    sf = f"{LOG_DIR}/latest_summary.txt"
    with open(sf, 'w') as f:
        f.write(msg)

    webhook_urls = os.environ.get("FEISHU_WEBHOOK_URL", "").split(",")
    for hook in webhook_urls:
        hook = hook.strip()
        if not hook:
            continue
        try:
            requests.post(
                hook,
                json={
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {
                                "tag": "plain_text",
                                "content": "📡 60分钟K线扫描 v3.0(JYFG)",
                            }
                        },
                        "elements": [
                            {"tag": "markdown", "content": msg}
                        ],
                    },
                },
                timeout=10,
            )
        except Exception as e:
            log(f"飞书推送失败: {e}")


# ─── 主流程 ──────────────────────────────────

def scan_and_trade():
    t0 = time.time()
    log(f"60分钟K线扫描 v3.0 JYFG引擎 启动 (mode={MODE})")

    qsvc, tsvc = _load_trading_modules()

    # 查账户（仅full模式）
    acct = None
    positions = []
    pos_map = {}
    if IS_FULL:
        acct = get_account_info(qsvc)
        if not acct:
            log("❌ 查资金失败，以下单模式运行但无账户数据")
        positions = get_positions(qsvc)
        pos_map = {p['code']: p for p in positions}
        log(
            f"账户: 总资产{acct['total_asset']:.0f} "
            f"可用{acct['available']:.0f} 持仓{len(positions)}只"
        )
        if positions:
            for p in positions:
                log(
                    f"  {p['name']}({p['code']}) {p['shares']}股 "
                    f"成本{p['cost']:.2f} 市值{p['market_val']:.0f}"
                )
    else:
        log("🔍 扫描模式(仅检测信号，不下单)")

    # 全市场扫描
    all_stocks = get_all_stocks()
    if not all_stocks:
        log("❌ 无行情数据")
        push_feishu(f"⏰ {now.strftime('%Y-%m-%d %H:%M')}\n❌ 获取行情数据失败，扫描跳过")
        return

    candidates = sorted(
        [s for s in all_stocks if s['turnover'] > 2.0],
        key=lambda x: x['turnover'],
        reverse=True,
    )[:120]
    log(f"全市场{len(all_stocks)}只，筛选换手>2%共{len(candidates)}只")

    trade_log = []

    # ── 买入扫描 ──
    buy_signals = []
    for idx, s in enumerate(candidates):
        if not check_listing(s['code']):
            continue
        if s['code'] in pos_map:
            continue
        if s['change_pct'] >= 8:
            continue
        if s['price'] <= 0:
            continue

        klines = get_60min_kline(s['code'])
        if not klines:
            continue

        ind = compute_jyfg_indicators(klines)

        # 趋势过滤
        if not ind['uptrend'] and ind['score'] < 50:
            continue
        if not ind['has_buy']:
            continue

        buy_signals.append({
            'code': s['code'],
            'name': s['name'],
            'price': s['price'],
            'change_pct': s['change_pct'],
            'turnover': s['turnover'],
            'score': ind['score'],
            'buy_grade': ind['buy_grade'],
            'buy_type': ind['buy_type'],
            'stop_price': round(ind['stop_price'], 2),
            'uptrend': ind['uptrend'],
        })
        log(
            f"  🟢 {s['name']}({s['code']}) {ind['buy_type']} "
            f"评分{ind['score']} 价{s['price']:.2f} 涨{s['change_pct']:+.2f}%"
        )

        # ── 自动买入（仅full模式） ──
        if IS_FULL and acct and tsvc:
            cur_hold = len(positions) + len(
                [t for t in trade_log if t['action'] == '买入']
            )
            if cur_hold >= MAX_HOLDINGS:
                log(f"⚠️ 已达最大持仓{MAX_HOLDINGS}只，跳过{s['name']}")
                continue

            buy_ratio = get_buy_ratio(ind)
            qty = max(
                int(acct['total_asset'] * buy_ratio / s['price'] / 100) * 100, 100
            )
            if qty < 100:
                continue

            log(
                f"🟢 [{ind['buy_type']}]买入 {s['name']}({s['code']}) "
                f"评分{ind['score']} {s['price']:.2f}×{qty}={qty*s['price']:.0f} "
                f"止损{ind['stop_price']:.2f}"
            )
            result = place_buy_order(tsvc, s['code'], s['price'], qty)
            if result.get('success'):
                trade_log.append({
                    'action': '买入',
                    'code': s['code'],
                    'name': s['name'],
                    'price': s['price'],
                    'qty': qty,
                    'amount': round(qty * s['price'], 2),
                    'status': '成交',
                    'buy_type': ind['buy_type'],
                    'score': ind['score'],
                    'stop_price': round(ind['stop_price'], 2),
                })
                log(f"  ✅ 委托{result.get('entrust_no','')}")
            else:
                trade_log.append({
                    'action': '买入',
                    'code': s['code'],
                    'name': s['name'],
                    'price': s['price'],
                    'qty': qty,
                    'status': '失败',
                    'error': result.get('error', ''),
                })
                log(f"  ❌ {result.get('error','')}")

        if (idx + 1) % 30 == 0:
            log(f"  进度{idx+1}/{len(candidates)}")

    # ── 持仓扫描：卖出（仅full模式） ──
    sell_details = []
    if IS_FULL:
        for pos in positions:
            code = pos['code']
            klines = get_60min_kline(code)
            if not klines:
                continue

            ind = compute_jyfg_indicators(klines)
            shares = pos['shares']
            if shares <= 0:
                continue

            # 获取实时卖一价
            try:
                pre = 'sh' if code.startswith('6') else 'sz'
                r = requests.get(f"http://qt.gtimg.cn/q={pre}{code}", timeout=5)
                f = r.text.split('~')
                bid1 = (
                    float(f[9])
                    if len(f) > 9 and f[9] and float(f[9]) > 0
                    else 0
                )
                cur_price = float(f[3]) if len(f) > 3 and f[3] else 0
            except:
                bid1, cur_price = 0, 0

            sell_price = (
                bid1 if bid1 > 0
                else (cur_price if cur_price > 0 else pos.get('cost', 0))
            )

            sell_action = None
            sell_reason = ''
            if ind['stop_loss']:
                sell_action = '止损'
                sell_reason = f"跌破动态止损{ind['stop_price']:.2f}"
            elif ind['high_profit']:
                sell_action = '止盈'
                sell_reason = f"高位止盈(RSI{ind['rsi6']:.0f}顶背离)"
            elif ind['trend_broken']:
                sell_action = '短卖'
                sell_reason = f"趋势破坏(破EMA34)"
            elif ind['momentum_fade']:
                sell_action = '短卖'
                sell_reason = f"动能衰减(MACD死叉)"
            elif ind['sell_grade'] >= 2 and ind['score'] < 40:
                sell_action = '短卖'
                sell_reason = f"评分降至{ind['score']}"

            if sell_action and tsvc:
                log(
                    f"{'🔴' if sell_action=='止损' else '🟡'} {sell_action} "
                    f"{pos['name']}({code}) {shares}股 原因:{sell_reason} 评分{ind['score']}"
                )
                result = place_sell_order(tsvc, code, sell_price, shares)
                if result.get('success'):
                    trade_log.append({
                        'action': sell_action,
                        'code': code,
                        'name': pos['name'],
                        'price': sell_price,
                        'qty': shares,
                        'amount': round(sell_price * shares, 2),
                        'status': '成交',
                        'reason': sell_reason,
                    })
                    log(f"  ✅ 卖单{result.get('entrust_no','')}")
                else:
                    trade_log.append({
                        'action': sell_action,
                        'code': code,
                        'name': pos['name'],
                        'price': sell_price,
                        'qty': shares,
                        'status': '失败',
                        'error': result.get('error', ''),
                    })

                sell_details.append(
                    f"{'🔴' if sell_action=='止损' else '🟡'} "
                    f"{pos['name']}({code}) {sell_action} "
                    f"{sell_price:.2f}×{shares} | {sell_reason}"
                )

    cost = time.time() - t0

    # ── 飞书推送 ──
    lines = [f"📡 **60分钟K线扫描 v3.0 JYFG引擎**"]
    lines.append(
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} | 耗时{cost:.0f}s | 模式:{MODE.upper()}"
    )
    if acct:
        lines.append(
            f"💰 总资产{acct['total_asset']:.0f} 可用{acct['available']:.0f} "
            f"持仓{len(positions)}只"
        )
    lines.append("")

    if trade_log:
        lines.append("**交易记录:**")
        for t in trade_log:
            if t['action'] == '买入':
                bt = t.get('buy_type', '')
                sc = t.get('score', 0)
                sp = t.get('stop_price', 0)
                lines.append(
                    f"✅ 🟢 {t['name']}({t['code']}) {bt} 评分{sc} "
                    f"{t['price']:.2f}×{t['qty']}={t['amount']:.0f} 止损{sp}"
                )
            else:
                r = t.get('reason', '')
                lines.append(
                    f"✅ {'🔴' if t['action']=='止损' else '🟡'} "
                    f"{t['name']}({t['code']}) {t['action']} "
                    f"{t['price']:.2f}×{t['qty']}={t['amount']:.0f} {r}"
                )
        lines.append("")

    if buy_signals:
        lines.append(
            f"**🟢 买入信号: {len(buy_signals)}只**"
            f"{' (扫描模式，未下单)' if not IS_FULL else ''}"
        )
        for s in buy_signals[:10]:
            lines.append(
                f"  {s['name']}({s['code']}) {s['buy_type']} 评分{s['score']} "
                f"价{s['price']:.2f} 涨{s['change_pct']:+.2f}% 止损{s['stop_price']}"
            )
        lines.append("")
    elif not trade_log:
        lines.append("✅ 无信号触发")
        lines.append("")

    buy_count = sum(1 for t in trade_log if t['action'] == '买入')
    sell_count = sum(1 for t in trade_log if t['action'] != '买入')
    stop_count = sum(1 for t in trade_log if t['action'] == '止损')
    lines.append(
        f"📊 扫描{len(all_stocks)}只 | 买入信号{buy_count+len(buy_signals)}个 "
        f"| 买入{buy_count}笔 | 卖出{sell_count}笔(止损{stop_count})"
    )
    if not IS_FULL:
        lines.append("")
        lines.append("*运行模式: scan (仅扫描信号)*")

    msg = '\n'.join(lines)
    push_feishu(msg)
    print(f"\n{msg}")

    # 保存日志
    result_log = {
        'scan_time': now.strftime('%Y-%m-%d %H:%M:%S'),
        'version': 'v3.0 JYFG',
        'mode': MODE,
        'cost_sec': round(cost, 1),
        'total_asset': acct['total_asset'] if acct else 0,
        'available': acct['available'] if acct else 0,
        'positions': len(positions) if acct else 0,
        'buy_signal_count': len(buy_signals),
        'trades': trade_log,
        'sell_details': sell_details,
        'buy_signals': buy_signals[:10],  # 只存前10个
    }
    with open(
        f"{LOG_DIR}/scan_{now.strftime('%Y%m%d_%H%M')}.json", 'w'
    ) as f:
        json.dump(result_log, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    h, m = now.hour, now.minute
    in_trading_time = (
        (9 <= h < 11) or (h == 11 and m <= 30)
        or (13 <= h < 15) or (h == 15 and m <= 5)
    )
    if '--force' in sys.argv:
        in_trading_time = True
    if not in_trading_time and not IS_CLOUD:
        print("⏰ 非交易时间，跳过")
        sys.exit(0)
    scan_and_trade()
