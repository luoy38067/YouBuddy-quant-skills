"""桥水全天候策略（BridgeWater_AllWeather）回测入口。

Usage:
    python backtest.py <run_manifest.json>

策略算法、调仓规则与撮合口径详见 SKILL.md。

数据口径：交易日历、四只 ETF 的日频行情与基准均由宿主运行时提供（inputs/）。
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date as _date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Inputs, _write_csv, compute_metrics, write_json  # noqa: E402

# 与源脚本常量一致（写死，不暴露参数）。
INITIAL_CASH = 1_000_000.0
COMMISSION = 0.0003       # 佣金，双边；ETF 无印花税
LOOKBACK_DAYS = 120       # 信号回看交易日数
ES_ALPHA = 0.01           # 历史期望损失分位
ES_SAMPLE_MIN = 10        # 样本下限（不足返回保护值）
ES_FLOOR = 0.001          # |ES| 数值保护下限（防除零）
WEIGHT_CAP = 0.40         # 单资产权重上限
DIFF_THRESHOLD = 200.0    # 差额调仓触发阈值（元）

POOL = ["515220", "511260", "518880", "512890"]


def _is_week_boundary(last_bar: Optional[str], date: str) -> bool:
    # 与上一次行情 bar 间隔 >=3 天视为跨周（含周末/长假），本周首个交易日再平衡。
    if last_bar is None:
        return True
    delta = (_date.fromisoformat(date) - _date.fromisoformat(last_bar)).days
    return delta >= 3


def _round_lots(x: float) -> int:
    # 四舍五入取整（与原始策略一致）。
    return math.floor(x + 0.5)


# ──────────────────────────────────────────────────────────────
# 信号：逆 ES 风险平价目标权重
# ──────────────────────────────────────────────────────────────

def _es(returns: List[float]) -> float:
    """最差 1% 日收益均值；样本不足返回保护值。"""
    n = len(returns)
    if n < ES_SAMPLE_MIN:
        return ES_FLOOR
    ordered = sorted(returns)
    cutoff = int(ES_ALPHA * n)
    if cutoff > 0:
        return sum(ordered[:cutoff]) / cutoff
    return ordered[0]


def _cap_pass(weights: Dict[str, float], cap: float) -> Dict[str, float]:
    excess = 0.0
    for sym, w in weights.items():
        if w > cap:
            excess += w - cap
            weights[sym] = cap
    room = sum(w for w in weights.values() if w < cap)
    if excess > 1e-12 and room > 1e-12:
        for sym, w in weights.items():
            if w < cap:
                weights[sym] = w + excess * (w / room)
    return weights


def _cap_weights(weights: Dict[str, float], cap: float) -> Dict[str, float]:
    total = sum(weights.values())
    if total <= 0.0:
        return weights
    weights = {s: w / total for s, w in weights.items()}
    for _ in range(5):
        weights = _cap_pass(weights, cap)
    return weights


def weekly_targets(inputs: Inputs, trade_date: str) -> Optional[Dict[str, float]]:
    """逆 ES 风险平价目标权重；有效资产 <2 返回 None（保持持仓不调仓）。"""
    series: Dict[str, List[Tuple[str, float]]] = {}
    valid: List[str] = []
    for sym in POOL:
        t = inputs.closes_before(sym, trade_date)
        if len(t) >= 20:
            series[sym] = t
            valid.append(sym)
    if len(valid) < 2:
        return None

    # 多资产共同交易日交集（保持首个有效资产的日期序），取最近 120 日
    common = [d for d, _ in series[valid[0]]]
    for sym in valid[1:]:
        sym_days = set(d for d, _ in series[sym])
        common = [d for d in common if d in sym_days]
    if len(common) > LOOKBACK_DAYS:
        common = common[-LOOKBACK_DAYS:]
    if len(common) < 2:
        return None
    common_set = set(common)

    es: Dict[str, float] = {}
    for sym in valid:
        pairs = [(d, c) for d, c in series[sym] if d in common_set]
        if len(pairs) >= 2:
            rets = []
            for i in range(1, len(pairs)):
                rets.append(pairs[i][1] / pairs[i - 1][1] - 1.0)
            if len(rets) >= ES_SAMPLE_MIN:
                es_val = _es(rets)
                if es_val == 0.0:
                    es_val = ES_FLOOR
                es[sym] = abs(es_val)
    if len(es) < 2:
        return None

    total_inv = sum(1.0 / v for v in es.values())
    weights = {sym: (1.0 / v) / total_inv for sym, v in es.items()}
    return _cap_weights(weights, WEIGHT_CAP)


# ──────────────────────────────────────────────────────────────
# 模拟主流程
# ──────────────────────────────────────────────────────────────

class Backtest:
    def __init__(self, manifest_path: str):
        manifest_path = Path(manifest_path)
        self.run_dir = manifest_path.parent
        with manifest_path.open("r", encoding="utf-8-sig") as handle:
            self.manifest = json.load(handle)
        self.run_id = str(self.manifest.get("run_id") or "")
        params = dict(((self.manifest.get("parameters") or {}).get("structured") or {}))
        self.start = str(params.get("start") or "")
        requested_end = str(params.get("end") or "")
        data_context = dict(self.manifest.get("data_context") or {})
        effective_end = str(data_context.get("effective_end") or
                            data_context.get("data_cutoff") or requested_end)
        self.end = min(requested_end, effective_end) if effective_end else requested_end
        self.inputs = Inputs(self.run_dir)
        self.dates = [d for d in self.inputs.calendar if self.start <= d <= self.end]
        self.staging = self.run_dir / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        # 组合状态：pos 为有序 dict，键序 = 建仓顺序（新标的追加表尾）
        self.cash = INITIAL_CASH
        self.pos: Dict[str, int] = {}
        self.last_bar: Optional[str] = None
        # 产物
        self.nav_rows: List[Dict[str, object]] = []
        self.trade_rows: List[Dict[str, object]] = []
        self.record_rows: List[Dict[str, object]] = []
        self.event_seq = 0
        self.order_seq = 0

    def prev_trading_day(self, date: str) -> Optional[str]:
        index = self.inputs.calendar.index(date) if date in self.inputs.calendar else -1
        return self.inputs.calendar[index - 1] if index > 0 else None

    # ── 调仓（源脚本 onBar：跨周首个交易日）──

    def rebalance(self, date: str) -> None:
        weights = weekly_targets(self.inputs, date)
        if weights is None or len(weights) < 2:
            return

        def opx(ticker: str) -> Optional[float]:
            # 仅当日在册行情可成交（msg.keys() 语义），停牌缺失不得伪造。
            price = self.inputs.open_price(ticker, date)
            return price if price is not None and price > 0 else None

        # tot 口径：当日收盘估值（停牌用最近可得收盘；完全无行情计 0）。
        pos_mv = 0.0
        for ticker, qty in self.pos.items():
            price = (self.inputs.close(ticker, date)
                     or self.inputs.last_close_at_or_before(ticker, date) or 0.0)
            pos_mv += qty * price
        tot = self.cash + pos_mv
        if not math.isfinite(tot) or tot <= 0:
            return

        signal_date = self.prev_trading_day(date) or date
        orders: List[Tuple[str, str, int, float, str, float]] = []
        for ticker in POOL:                              # 按标的池固定顺序逐单提交
            if ticker not in weights:
                pos_qty = self.pos.get(ticker, 0)
                if pos_qty > 0:
                    price = opx(ticker)
                    if price is None:
                        continue                         # 停牌无行情：不下单
                    orders.append(("sell", ticker, pos_qty, price, "clear", 0.0))
            else:
                price = opx(ticker)
                if price is None:
                    continue                             # 停牌无行情：跳过
                wt = weights[ticker]
                target_val = tot * wt
                cur_qty = self.pos.get(ticker, 0)
                diff = target_val - cur_qty * price
                if diff > DIFF_THRESHOLD:
                    lots = _round_lots(diff / price / 100) * 100   # 四舍五入整百
                    if lots > 0:
                        orders.append(("buy", ticker, lots, price, "rebalance", wt))
                elif diff < -DIFF_THRESHOLD:
                    sqty = _round_lots(-diff / price / 100) * 100
                    if sqty > cur_qty:
                        sqty = cur_qty
                    if sqty > 0:
                        orders.append(("sell", ticker, sqty, price, "rebalance", wt))
        if not orders:
            return

        self.event_seq += 1
        event_id = f"{date}-scheduled-{self.event_seq:04d}"

        # ── 撮合（按订单提交顺序逐单处理）：卖单即时成交回款；买单按
        #    提交时点可用现金检查（金额+佣金不足整单跳过），通过即预扣
        #    现金，随后成交并更新持仓（新标的建仓追加）。
        passed: List[Tuple[str, int, float]] = []
        for typ, ticker, qty, price, label, wt in orders:
            if typ == "sell":
                fee = qty * price * COMMISSION
                self.cash += qty * price - fee
                old_qty = self.pos.get(ticker, 0)
                self.pos[ticker] = old_qty - qty
                if self.pos[ticker] <= 0:
                    del self.pos[ticker]                 # 清仓删行，其余相对序不变
                self.trade_rows.append({
                    "date": date, "ticker": ticker, "side": "sell",
                    "quantity": f"{qty:.4f}", "price": f"{price:.6f}", "fee": f"{fee:.8f}"})
                self.record_rows.append({
                    "event_id": event_id, "signal_date": signal_date,
                    "execution_date": date, "trigger_type": "scheduled",
                    "trigger_reason": "weekly_first_trading_day_rebalance", "ticker": ticker,
                    "old_weight": f"{old_qty * price / tot:.6f}",
                    "target_weight": f"{wt:.6f}" if label == "rebalance" else "0.000000",
                    "actual_weight": f"{self.pos.get(ticker, 0) * price / tot:.6f}"})
            else:
                cost = qty * price * (1 + COMMISSION)
                if cost > self.cash:
                    continue                             # 资金不足整单跳过
                self.cash -= cost                        # 预扣现金
                passed.append((ticker, qty, price))
        for ticker, qty, price in passed[::-1]:          # 通过检查的买单依次成交（与原始策略成交顺序一致）
            old_qty = self.pos.get(ticker, 0)
            self.pos[ticker] = old_qty + qty             # 新标的建仓追加
            fee = qty * price * COMMISSION
            self.trade_rows.append({
                "date": date, "ticker": ticker, "side": "buy",
                "quantity": f"{qty:.4f}", "price": f"{price:.6f}", "fee": f"{fee:.8f}"})
            self.record_rows.append({
                "event_id": event_id, "signal_date": signal_date,
                "execution_date": date, "trigger_type": "scheduled",
                "trigger_reason": "weekly_first_trading_day_rebalance", "ticker": ticker,
                "old_weight": f"{old_qty * price / tot:.6f}",
                "target_weight": f"{weights.get(ticker, 0.0):.6f}",
                "actual_weight": f"{(old_qty + qty) * price / tot:.6f}"})

    # ── 逐日净值（收盘价盯市，停牌用最近可得收盘）──

    def mark(self, date: str) -> float:
        value = self.cash
        for ticker, qty in self.pos.items():
            price = self.inputs.last_close_at_or_before(ticker, date)
            if price is not None:
                value += qty * price
        return value

    def run(self) -> Dict[str, object]:
        nav_values: List[float] = []
        for date in self.dates:
            if _is_week_boundary(self.last_bar, date):
                self.rebalance(date)
            self.last_bar = date
            nav_values.append(self.mark(date))
        return self._write_outputs(nav_values)

    # ── 产物输出（与宿主 verifier 对齐的产物结构）──

    def _write_outputs(self, nav_values: List[float]) -> Dict[str, object]:
        nav0 = nav_values[0] if nav_values and nav_values[0] > 0 else 1.0
        prev: Optional[float] = None
        for date, value in zip(self.dates, nav_values):
            daily_return = 0.0 if not prev else value / prev - 1.0
            prev = value
            self.nav_rows.append({
                "date": date,
                "nav_value": value / nav0,
                "strategy_nav": f"{value / nav0:.12f}",
                "daily_return": f"{daily_return:.12f}"})
        metrics = compute_metrics(self, nav_values)  # 只用比例，归一化无影响
        write_json(self.staging / "metrics.json", {"contract": "strategy.metrics.v1", **metrics})
        _write_csv(self.staging / "daily_nav.csv",
                   ["date", "strategy_nav", "daily_return"], self.nav_rows)
        self._write_benchmark_nav()
        self._write_holdings(nav_values)
        _write_csv(self.staging / "trades.csv",
                   ["date", "ticker", "side", "quantity", "price", "fee"], self.trade_rows)
        _write_csv(self.staging / "rebalance_records.csv",
                   ["event_id", "signal_date", "execution_date", "trigger_type",
                    "trigger_reason", "ticker", "old_weight", "target_weight",
                    "actual_weight"], self.record_rows)
        summary = {
            "strategy": self.describe(),
            "start": metrics.get("start"),
            "end": metrics.get("end"),
            "total_return": metrics.get("total_return"),
            "annualized_return": metrics.get("annualized_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "sharpe": metrics.get("sharpe"),
            "benchmark_total_return": metrics.get("benchmark_total_return"),
            "trading_days": metrics.get("trading_days"),
            "trade_count": len(self.trade_rows),
            "rebalance_events": len({row["event_id"] for row in self.record_rows}),
            "fee_total": metrics.get("total_fee"),
        }
        result = {
            "contract": "strategy_result.v1",
            "status": "computed",
            "run_id": self.run_id,
            "summary": summary,
            "output_files": ["metrics.json", "daily_nav.csv", "benchmark_nav.csv",
                             "latest_holdings.csv", "trades.csv", "rebalance_records.csv"],
        }
        write_json(self.staging / "strategy_result.json", result)
        return result

    def _write_benchmark_nav(self) -> None:
        rows: List[Dict[str, object]] = []
        base: Optional[float] = None
        for row in self.nav_rows:
            date = str(row["date"])
            close = self.inputs.benchmark_close_at_or_before(date)
            if close is not None and base is None:
                base = close
            rows.append({"date": row["date"],
                         "benchmark_nav": f"{close / base:.12f}"
                         if close is not None and base is not None and base > 0 else ""})
        _write_csv(self.staging / "benchmark_nav.csv", ["date", "benchmark_nav"], rows)

    def _write_holdings(self, nav_values: List[float]) -> None:
        date = self.dates[-1] if self.dates else ""
        rows: List[Dict[str, object]] = []
        total = self.mark(date) if self.dates else 0.0
        if total > 0:
            for ticker in sorted(self.pos):
                price = self.inputs.last_close_at_or_before(ticker, date)
                if price is not None:
                    value = self.pos[ticker] * price
                    rows.append({"ticker": ticker,
                                 "weight": f"{value / total:.6f}", "as_of": date})
        _write_csv(self.staging / "latest_holdings.csv", ["ticker", "weight", "as_of"], rows)

    def describe(self) -> Dict[str, object]:
        return {
            "id": "BridgeWater_AllWeather",
            "name": "桥水全天候策略（四资产逆ES风险平价周频）",
            "universe": "煤炭 / 城投债 / 黄金 / 红利低波四只 ETF（固定池）",
            "rebalance": "每周首个交易日（跨周间隔>=3天识别），开盘价撮合、收盘价估值",
            "position_ratio": 1.0,
            "initial_cash": INITIAL_CASH,
            "commission": COMMISSION,
            "tax": 0.0,
            "embedded_data": [],
            "matching": ("执行日开盘价整百股撮合（卖单即时回款、买单现金不足整单跳过、"
                         "差额阈值以下不动；无涨跌停拦截，与原始策略一致）"),
        }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: backtest.py <run_manifest.json>", file=sys.stderr)
        return 2
    backtest = Backtest(sys.argv[1])
    result = backtest.run()
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
