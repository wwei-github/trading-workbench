# v1.0.0 Release Manifest

## 版本事实

| 项 | 值 |
|---|---|
| git tag | `v1.0.0` |
| commit | `d16ae602ddbe91d330f5c50f6d1997745831c66a` |
| 分支 | `release/v1`（仅缺陷修复） |
| 镜像 | `trading-workbench-backend:v1.0.0` / `trading-workbench-frontend:v1.0.0` |
| STRATEGY_VERSION | `v1_4h_1h` |
| RULE_VERSION | `v2026-10-27` |

## 策略特征

| 项 | 值 |
|---|---|
| 趋势周期 | 4H（600 根） |
| 入场周期 | 1H（240 根，实际取 max(300)） |
| 单笔风险 | 风险基数 × 3% |
| 风险基数 | 钱包首位数取整，上限 5000 |
| 熔断 | 日 9% / 周 15% / 月 25% |
| 最大在跑单 | 5 |
| 扫描节拍 | 每小时 `:00` (UTC) |
| 结算巡检 | 每 30 分钟 `:01/:31` |

## system_config 快照

**必须在有 DB 访问的机器上导出**（本地无服务器连接，未执行）：

```bash
psql "$DATABASE_URL" -c "\copy (SELECT * FROM system_config) TO 'system_config_v1.0.0.csv' CSV HEADER"
```

导出后放到本目录，并记录 `decision_version` 值：

| 项 | 值 |
|---|---|
| decision_version | 待导出时填写 |

## .env 脱敏摘要

只记录键名与是否设置，**不记录密钥值**：

- `BINANCE_API_KEY` / `BINANCE_API_SECRET`：已设置
- `BINANCE_TESTNET`：当前值待现场确认
- `LLM_*`：已设置
- `DATABASE_URL` / `REDIS_URL`：指向 `trading_workbench` / `6380`

## 回退命令

```bash
./scripts/release.sh --tag v1.0.0
```
