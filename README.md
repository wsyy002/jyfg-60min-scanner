# JYFG 60分钟K线扫描器 v3.0

基于同花顺JYFG波段买卖指标公式 GPT Wave V2.1 Lite 重构的60分钟K线扫描系统。

## 数据源

- **实时行情**: 腾讯 qt.gtimg.cn
- **60分钟K线**: akshare(东方财富) → Sina(备用)
- **股票清单**: akshare stock_zh_a_spot

## 运行模式

### 1. 云端扫描模式 (默认)
GitHub Actions 云端运行，只扫描信号，不下单，结果推飞书。

```bash
python 60min_scanner_ga.py
```

### 2. 完整模式 (含下单)
需在本地 NAS 安装 Self-Hosted GitHub Actions Runner。

```bash
JYFG_MODE=full python 60min_scanner_ga.py
```

## GitHub Actions 配置

1. 在仓库 Settings → Secrets and variables → Actions 添加：
   - `FEISHU_WEBHOOK_URL`: 飞书机器人 Webhook 地址
2. Workflow 自动按以下时间运行（北京时间）：
   - 10:31 / 11:31 / 14:01 / 15:01（交易日）

## Self-Hosted Runner 安装（如需下单）

```bash
# 在 NAS 上运行
mkdir -p ~/actions-runner && cd ~/actions-runner
# 下载 runner（从 GitHub 仓库 Settings → Actions → Runners 获取命令）
# 配置并启动
./config.sh --url https://github.com/wsyy002/jyfg-60min-scanner --token YOUR_TOKEN
./run.sh
# 配置为服务
sudo ./svc.sh install
sudo ./svc.sh start
```

## 本地安装

```bash
pip install -r requirements.txt
FEISHU_WEBHOOK_URL=your_webhook python 60min_scanner_ga.py
```

## 技术指标

- EMA13/34/55/89 趋势系统
- MACD 动量
- KDJ 超买超卖
- RSI6 强度
- ATR14 动态止损
- 成交量异常检测
- 趋势评分 (0-100)

## 买入信号

| 类型 | 等级 | 条件 |
|------|------|------|
| 突破买 | ⭐⭐⭐ | 放量突破20周期高点 + EMA13之上 |
| 趋势启动 | ⭐⭐ | EMA13上穿EMA34 + MACD多头+放量 |
| 回踩买 | ⭐ | 价格回踩EMA13后弹起 |
| KDJ金叉 | ⭐(辅助) | KDJ金叉+评分≥40 |

## 卖出条件

| 类型 | 优先级 | 条件 |
|------|--------|------|
| 止损 | 🔴 | 跌破ATR动态止损线 |
| 高位止盈 | 🟡 | 创新高+RSI超买+MACD顶背离 |
| 趋势破坏 | ⚠️ | 破EMA34+EMA13拐头 |
| 动能衰减 | ⚠️ | MACD死叉+价格在EMA13下 |
