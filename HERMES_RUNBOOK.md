# Hermes BTC 15m 部署说明

这是 BTC 15分钟预测系统的节点部署包。默认只读影子模式，不包含任何密钥，
也不会提交真实订单。

## 内含策略

- `stable_original`：稳健推荐原版，默认候选。
- `value_original`：价值平衡原版，高收益候选。
- 31模型共识预测、Binance 1分钟数据、预测市场发现探针。
- 固定10U、连续亏损复核、极端行情熔断和结果导出。

## 安全规则

1. 使用专用 Binance API，关闭提现权限。
2. 将 Hermes 云服务器固定公网 IP 加入 Binance 白名单。
3. 密钥只写入服务器本地 `.env`，权限设为 `600`，禁止提交 Git。
4. 首次运行保持 `TRADING_MODE=SHADOW`。
5. 在市场映射、赔率、结算和重复订单保护验证前，不得启用真实交易。

## 安装

```bash
unzip btc15_hermes_bundle.zip
cd btc15_hermes_bundle
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r app/requirements.txt
cp .env.example .env
chmod 600 .env
```

在服务器本地编辑 `.env`：

```text
BINANCE_API_KEY=你的API Key
BINANCE_API_SECRET=你的Secret Key
```

不要把 `.env` 内容发到聊天、GitHub或回测数据中。

## 第一步：只读市场验证

```bash
set -a
source .env
set +a
python binance_prediction_probe.py
```

成功后生成 `binance_prediction_snapshot.json`。Hermes需要确认标题包含BTC 15分钟涨跌，
并保存 `vendor / marketTopicId / marketId / tokenId` 等字段。

## 模型回放

```bash
python app/fetch_binance.py --days 105 --out BTCUSDT_1m.csv
python app/polymarket_15m.py --csv BTCUSDT_1m.csv --decision-minute 5 --min-train 672 --out pm15
```

## 交给Hermes的任务文本

```text
在固定IP节点部署此目录。保持TRADING_MODE=SHADOW，禁止提交真实订单。
每15分钟第2分钟执行binance_prediction_probe.py，记录BTC 15分钟市场映射；
每轮保存预测时间、策略名、UP/DOWN、票数、概率、买入价、是否成交、结算结果、
盈亏、连续亏损、暂停原因。每日北京时间08:15输出脱敏CSV和JSON汇总。
任何日志不得包含API Key、Secret、签名或请求完整查询串。
```

## 返回优化的数据

只需返回以下文件，不要返回 `.env`：

- `shadow_trades.csv`
- `daily_summary.json`
- `binance_prediction_snapshot.json`（先检查不含敏感字段）

建议至少积累2天影子数据，再评估是否编写真正的下单适配器；半个月数据用于重新检查
胜率、收益、最大回撤、实际滑点和连续亏损分布。
