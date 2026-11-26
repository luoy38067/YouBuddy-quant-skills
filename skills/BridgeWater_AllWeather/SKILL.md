---
name: BridgeWater_AllWeather
flow_version: 2
kind: strategy_backtest
contract: strategy_backtest.v1
rebalance:
  mode: scheduled
  signal_timing: close
  execution_timing: next_open
params:
  start: {type: date, required: false, default: "2024-01-02"}
  end: {type: date, required: false, default: "2026-11-10"}
  universe: {type: enum, default: CUSTOM}
  benchmark: {type: string, default: "000300"}
---

# 桥水全天候策略

本 skill 复刻原始策略《桥水全天候策略》（BridgeWater_AllWeather）：
在煤炭、城投债、黄金、红利低波四只 ETF 组成的固定资产池内，按各资产
近 120 个交易日收益的尾部风险（1% 历史期望损失 ES）倒数分配权重
（风险平价思想，满仓无杠杆，单资产权重上限 40%），每周首个交易日
开盘价调仓、收盘价估值。交易日历、四只 ETF 的日频行情与沪深300 基准
收盘由宿主运行时提供（inputs/，构建器按信号预热窗口预取行情）；
Python 只做策略计算，不接触网络与数据服务凭据。

## 一、策略逻辑

- 资产池：固定四只 ETF——515220（煤炭）、511260（城投债）、
  518880（黄金）、512890（红利低波），权重含义为风险均衡配置。
- 调仓触发：与上一次行情 bar 间隔 ≥3 天视为跨周（含周末/长假），
  于本周首个交易日调仓（与源脚本一致）。
- 信号（全部取截至调仓日前一交易日的数据，杜绝未来函数）：
  - 各资产取收盘序列（严格早于调仓日），做多资产共同交易日交集，
    取最近 120 个交易日；有效资产（历史 ≥20 日）不足 2 只时不调仓；
  - 每资产按共同交易日收盘计算日收益率，取最差 1% 日收益的均值
    作为历史期望损失 ES（1% 分位对应样本数不足 10 时返回保护值）；
  - 权重 ∝ 1/|ES|，归一化使总权重 = 1（满仓，无杠杆）；
  - 单资产权重上限 40%：超过部分按未达上限资产的权重比例回补，
    迭代 5 次（与源脚本 capWeights/capPass 一致）；
  - |ES| 数值保护下限 0.001（防除零，与源脚本一致）。
- 差额调仓：目标市值与当前市值差额超过 200 元才下单；
  目标池外持仓清仓；有效资产不足或无差额时保持持仓不动。

## 二、执行流（Workflow v2）

1. `build_dataset`：宿主数据集构建器 `equity_backtest_bundle.v1` 拉取
   交易日历、ETF 日频行情与基准日行情，写入 `inputs/` 并生成数据指纹；
2. `run_backtest`：Python hook `backtest.py` 读取 `run_manifest.json`
   与 `inputs/`（交易日历、ETF 行情、基准），计算并撮合，产出 `staging/` 七类
   产物，stdout 输出 `strategy_result.v1`（status=computed）；
3. `validate_outputs`：宿主 verifier 按 `strategy_backtest.v1` 做文件级/
   数据级/交叉一致性校验；
4. `commit_run`：宿主把 staging 原子迁移到 outputs，登记行数 + SHA-256，
   状态机置 done（唯一写 done 的路径）。

## 三、撮合与费用口径

- 声明口径 `signal_timing=close / execution_timing=next_open`：
  rebalance_records 中 signal_date=前一交易日、execution_date=本周
  首个交易日（满足 verifier 勾稽）；实际撮合用**执行日开盘价**、
  估值用**执行日收盘价**（与原始策略一致）。
- 仓位核算：先用当日收盘计算持仓市值（停牌票用最近可得收盘，完全无
  行情计 0），tot = cash + posMV，目标市值 = tot × 权重。
- 订单执行顺序：按资产池固定顺序逐单提交（清仓卖单与差额调仓单
  交错），卖单即时成交回款；可用现金不足（金额+佣金）的买单整单
  跳过；整百股取整（四舍五入）。
- 涨跌停：与原始策略一致，不做涨跌停拦截。
- 费用：佣金 0.0003 双边、ETF 无印花税（与源脚本一致，写死）。

## 四、输出

- `outputs/daily_nav.csv`：每日净值与收益率；
- `outputs/metrics.json`：收益/波动/回撤/夏普等（strategy.metrics.v1）；
- `outputs/trades.csv`：逐笔成交；
- `outputs/rebalance_records.csv`：调仓事件流水；
- `outputs/latest_holdings.csv`：期末持仓；
- `outputs/benchmark_nav.csv`：基准净值（由宿主 benchmark.csv 提供）；
- `outputs/strategy_result.json`：运行结果摘要。

## 五、已知限制

- **行情复权口径**：宿主行情为前复权口径，与原始导出口径存在复权差异；
  512890 历史份额折算依赖复权价口径，复权方式不同会使整手取整路径
  与信号存在细微偏差；
- ETF 行情、交易日历与基准由宿主运行时供给，实际可得区间以宿主数据
  为准；信号预热不足（有效资产不足 2 只）时不调仓；
- 停牌（当日无行情）标的跳过下单；估值用最近可得收盘价；
- 现金不足跳过的买单不留成交记录（trades.csv 仅含实际成交）；
- 回测开始日早于 2024-01-02 的区间无原始净值可对照（原始脚本为
  固定窗口回放）。

LLM 收到 `completion_contract.no_more_tools=true` 后不得调用任何工具，
只按 `report.md` 模板生成回测解读报告。
