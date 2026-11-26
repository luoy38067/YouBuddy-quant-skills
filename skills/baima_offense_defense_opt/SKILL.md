---
name: baima_offense_defense_opt
flow_version: 2
kind: strategy_backtest
contract: strategy_backtest.v1
rebalance:
  mode: scheduled
  frequency: monthly
  calendar_rule: month_first_trading_day
  signal_timing: close
  execution_timing: next_open
params:
  start: {type: date, required: false, default: "2024-01-02"}
  end: {type: date, required: false, default: "2026-11-10"}
  universe: {type: enum, default: CSI300}
  benchmark: {type: string, default: "000300"}
---

# 沪深300白马攻防策略

本 skill 复刻原始策略《baima_offense_defense_opt_沪深300优选_baima》：
在沪深300成分股内按市场状态（冷/温/热三档）结合估值与盈利质量条件
筛选候选，再经价格动量过滤，按流动性（成交量）排序取前 5 只等权
持有，月度调仓（当月首个交易日）。每期调仓目标持仓表与目标股涨跌
停价内嵌自数据库导出，宿主只提供交易日历、个股/基准行情；Python
只做策略计算，不接触网络与数据服务凭据。

## 一、策略逻辑

- 股票池：沪深300成分股（剔除科创板/北交所/ST 及开盘触涨跌停个股，
  过滤已在内嵌目标持仓表中完成）。
- 调仓期次：`data/targets.csv` 内嵌 34 个调仓日（2024-01-02 ~
  2026-11-02，均为当月首个交易日）及每期目标持仓（顺序即源策略
  目标顺序，运行时不重排）。
- 选股逻辑（每期按市场状态分三档）：冷/温市筛选估值与盈利质量达标
  的白马股，热市筛选高景气成长股；候选经价格动量过滤后按成交量
  降序取前 5 等权。具体筛选阈值与因子构造不外露，已固化于内嵌
  目标持仓表。
- 2024-10 无符合条件个股，该月不调仓，持仓自然延续；
  2026-11-02 之后无新期次，组合自然漂移不再调仓。

## 二、执行流（Workflow v2）

1. `build_dataset`：宿主数据集构建器 `equity_backtest_bundle.v1` 拉取
   交易日历、股票池与个股/基准日行情，写入 `inputs/` 并生成数据指纹；
2. `run_backtest`：Python hook `backtest.py` 读取 `run_manifest.json`
   与 `inputs/`，结合内嵌数据计算并撮合，产出 `staging/` 七类产物，
   stdout 输出 `strategy_result.v1`（status=computed）；
3. `validate_outputs`：宿主 verifier 按 `strategy_backtest.v1` 做文件级/
   数据级/交叉一致性校验；
4. `commit_run`：宿主把 staging 原子迁移到 outputs，登记行数 + SHA-256，
   状态机置 done（唯一写 done 的路径）。

## 三、撮合与费用口径

- 调仓触发：每月首个交易日（且当日存在于内嵌目标表）触发一次；
  声明口径 `signal_timing=close / execution_timing=next_open`：
  rebalance_records 中 signal_date=前一交易日、execution_date=当月
  首个交易日，满足 verifier 勾稽；实际撮合用**执行日收盘价**（与
  原始策略一致）。
- 两阶段调仓：先清仓不在目标名单内的持仓（收盘价市价单，卖出资金
  当日回笼）；再按目标顺序整百股买入。
- 目标股数：按「现金 + 非目标持仓收盘估值」的等权金额计算全新目标
  股数（整百股四舍五入取整），对已持有的目标股直接增仓（持仓自然
  累加，与原始策略一致）。
- 拒单与涨跌停（与原始策略行为一致）：
  - 买单所需资金（金额+佣金）不足时整单拒单；
  - 收盘价 ≥ 涨停价的买单不成交（无现金、无费用、无持仓变动）；
  - 收盘价 ≤ 跌停价的卖单不成交（无回款、无费用、无持仓变动）；
  - 涨跌停价取自内嵌 `data/limits.csv`（前复权口径，仅覆盖调仓日
    目标股）。
- 费用：佣金 0.0003 双边、印花税 0.0005 仅卖出（与源脚本一致，写死）。
- 估值：逐日以收盘价盯市（前复权口径），停牌标的沿用最近可得收盘价。

## 四、输出

- `outputs/daily_nav.csv`：每日净值与收益率；
- `outputs/metrics.json`：收益/波动/回撤/夏普等（strategy.metrics.v1）；
- `outputs/trades.csv`：逐笔成交；
- `outputs/rebalance_records.csv`：调仓事件流水；
- `outputs/latest_holdings.csv`：期末持仓；
- `outputs/benchmark_nav.csv`：基准净值；
- `outputs/strategy_result.json`：运行结果摘要。

## 五、已知限制

- **行情复权口径**：本策略基于**前复权**行情（信号、撮合与估值同
  口径）；若宿主提供不复权或其他复权口径行情，两者个股价格口径
  不同，回测结果会有系统性偏差；
- 调仓目标持仓表与涨跌停价为固定内嵌数据（数据库导出），不随运行
  参数变化；调仓日晚于 2026-11-02 的区间无新调仓；
- 回测开始日晚于 2024-01-02 时，首期调仓沿用目标表首个覆盖期次；
  早于 2024-01-02 的区间无原始净值可对照（原始脚本为固定窗口回放）；
- 停牌（当日无行情）标的跳过成交与市值核算，次月按新目标重算；
- 现金不足的买单整单拒单，不产生成交记录（trades.csv 仅含实际成交）；
- 内嵌数据为数据库导出时点快照，数据可能事后修订，重导数据后结果
  随之同步。

LLM 收到 `completion_contract.no_more_tools=true` 后不得调用任何工具，
只按 `report.md` 模板生成回测解读报告。
