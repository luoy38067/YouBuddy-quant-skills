"""策略回测通用引擎（v1.1.0 strategy_backtest.v1）。

由各策略 skill 的 backtest.py 调用。引擎职责（与宿主 verifier 对齐）：

- 读取 run_manifest.json（argv[1]）中的结构化参数、调仓声明与 run_id；
- 读取宿主数据集构建器落盘的 canonical inputs（inputs/）：
    calendar.csv(date) / universe.csv(ticker,...) /
    daily_quote.csv(stock_code,trade_date,open,close,...) / benchmark.csv(date,close)；
- 事件驱动模拟：信号在 T 日收盘产生（signal_timing=close），T+1 开盘执行
  （execution_timing=next_open）；目标权重 -> 组合价值换算 -> 逐笔成交（含费率）；
- 产出 staging/ 产物：metrics.json / daily_nav.csv / benchmark_nav.csv /
  latest_holdings.csv / trades.csv / rebalance_records.csv / strategy_result.json；
- stdout 输出 strategy_result.v1（status 固定为 computed，done 只能由宿主写入）。

引擎只使用 Python 标准库，不做任何网络访问。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

TRADING_DAYS_PER_YEAR = 252
# 权重差异小于该比例视为无变化，不产生成交与记录。
WEIGHT_EPS = 1e-9


# ──────────────────────────────────────────────────────────────
# 输入装载
# ──────────────────────────────────────────────────────────────

class Inputs:
    """canonical inputs 的只读视图。"""

    def __init__(self, run_dir: Path):
        inputs = run_dir / "inputs"
        self.calendar: List[str] = _read_column(inputs / "calendar.csv", "date")
        self.universe: List[str] = []
        self.universe_periods: List[Tuple[str, str, str]] = []
        self._load_universe(inputs / "universe.csv")
        self.universe_set = set(self.universe)
        self.opens: Dict[str, Dict[str, float]] = {}
        self.closes: Dict[str, Dict[str, float]] = {}
        self._load_quotes(inputs / "daily_quote.csv")
        self.benchmark: Dict[str, float] = {}
        self._load_benchmark(inputs / "benchmark.csv")

    def _load_universe(self, path: Path) -> None:
        if not path.is_file():
            return
        tickers = set()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = (row.get("ticker") or row.get("stock_code") or "").strip()
                effective = (row.get("effective_date") or "").strip()
                end = (row.get("end_date") or "").strip()
                if ticker:
                    tickers.add(ticker)
                    self.universe_periods.append((ticker, effective, end))
        self.universe = sorted(tickers)

    def universe_at(self, date: str) -> List[str]:
        active = {ticker for ticker, effective, end in self.universe_periods
                  if (not effective or effective <= date) and (not end or date <= end)}
        return sorted(active) if active else list(self.universe)

    def _load_quotes(self, path: Path) -> None:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = (row.get("stock_code") or row.get("ticker") or "").strip()
                date = (row.get("trade_date") or row.get("date") or "").strip()
                if not ticker or not date:
                    continue
                open_px = _to_float(row.get("open"))
                close_px = _to_float(row.get("close"))
                if open_px is not None:
                    self.opens.setdefault(ticker, {})[date] = open_px
                if close_px is not None:
                    self.closes.setdefault(ticker, {})[date] = close_px

    def _load_benchmark(self, path: Path) -> None:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                date = (row.get("date") or "").strip()
                close = _to_float(row.get("close"))
                if date and close:
                    self.benchmark[date] = close

    def close(self, ticker: str, date: str) -> Optional[float]:
        return self.closes.get(ticker, {}).get(date)

    def open_price(self, ticker: str, date: str) -> Optional[float]:
        return self.opens.get(ticker, {}).get(date)

    def close_series(self, ticker: str, up_to: str, window: int) -> Optional[List[float]]:
        """截至 up_to（含）的最近 window 个收盘价；不足或有缺失返回 None。"""
        series = self.closes.get(ticker)
        if not series:
            return None
        dates = sorted(d for d in series if d <= up_to)
        tail = dates[-window:]
        if len(tail) < window:
            return None
        values = [series[d] for d in tail]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            return None
        return values

    def closes_before(self, ticker: str, date: str) -> List[Tuple[str, float]]:
        """trade_date < date 的 (日期, 收盘价) 升序序列（严格早于 date）。"""
        series = self.closes.get(ticker)
        if not series:
            return []
        dates = sorted(d for d in series if d < date)
        return [(d, series[d]) for d in dates]

    def last_close_at_or_before(self, ticker: str, date: str) -> Optional[float]:
        series = self.closes.get(ticker)
        if not series:
            return None
        earlier = [d for d in series if d <= date]
        if not earlier:
            return None
        return series[max(earlier)]

    def benchmark_close_at_or_before(self, date: str) -> Optional[float]:
        earlier = [day for day in self.benchmark if day <= date]
        return self.benchmark[max(earlier)] if earlier else None


def _read_column(path: Path, column: str) -> List[str]:
    if not path.is_file():
        return []
    values: List[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        key = _find_key(reader.fieldnames, column)
        if key is None:
            return []
        for row in reader:
            value = (row.get(key) or "").strip()
            if value:
                values.append(value)
    return values


def _find_key(fieldnames: Optional[List[str]], column: str) -> Optional[str]:
    if not fieldnames:
        return None
    lowered = {name.strip().lower(): name for name in fieldnames}
    return lowered.get(column.lower())


def _to_float(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ──────────────────────────────────────────────────────────────
# 调仓事件与组合
# ──────────────────────────────────────────────────────────────

@dataclass
class RebalanceEvent:
    """一次调仓事件：signal_date 收盘产生目标权重，execution_date 开盘执行。"""

    event_id: str
    signal_date: str
    execution_date: str
    trigger_type: str          # scheduled | signal | event
    trigger_reason: str
    # ticker -> 目标权重（0.0 表示清仓）。
    targets: Dict[str, float] = field(default_factory=dict)


class Portfolio:
    """基于份额的组合核算。初始净值 1.0 = 现金 1.0。"""

    def __init__(self, fee_rate: float):
        self.cash = 1.0
        self.shares: Dict[str, float] = {}
        self.entry_price: Dict[str, float] = {}
        self.fee_rate = fee_rate
        self.fee_total = 0.0

    def holdings_value(self, price_of: Callable[[str], Optional[float]]) -> float:
        total = 0.0
        for ticker, shares in self.shares.items():
            price = price_of(ticker)
            if price is not None:
                total += shares * price
        return total


# ──────────────────────────────────────────────────────────────
# 引擎主体
# ──────────────────────────────────────────────────────────────

class BacktestEngine:
    def __init__(self, manifest_path: str):
        self.manifest_path = Path(manifest_path)
        self.run_dir = self.manifest_path.parent
        with self.manifest_path.open("r", encoding="utf-8-sig") as handle:
            self.manifest = json.load(handle)
        self.run_id = str(self.manifest.get("run_id") or "")
        self.params: Dict[str, object] = dict(
            ((self.manifest.get("parameters") or {}).get("structured") or {}))
        self.rebalance: Dict[str, object] = dict(self.manifest.get("rebalance") or {})
        self.execution_timing = str(self.rebalance.get("execution_timing") or "next_open")
        fee_bps = _to_float(self.params.get("fee_bps")) or 0.0
        self.fee_rate = fee_bps / 10000.0
        self.inputs = Inputs(self.run_dir)
        self.staging = self.run_dir / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        # 回测主区间：交易日历 ∩ [start, end]。
        self.start = str(self.params.get("start") or "")
        requested_end = str(self.params.get("end") or "")
        data_context = dict(self.manifest.get("data_context") or {})
        effective_end = str(data_context.get("effective_end") or
                            data_context.get("data_cutoff") or requested_end)
        self.end = min(requested_end, effective_end) if effective_end else requested_end
        self.dates = [d for d in self.inputs.calendar if self.start <= d <= self.end]
        self.calendar_index: Dict[str, int] = {d: i for i, d in enumerate(self.inputs.calendar)}
        self.pending: List[RebalanceEvent] = []
        self.nav_rows: List[Dict[str, object]] = []
        self.trade_rows: List[Dict[str, object]] = []
        self.record_rows: List[Dict[str, object]] = []
        self.event_seq = 0
        self.holdings_snapshot: Dict[str, float] = {}

    # ── 日历工具 ──

    def next_trading_day(self, date: str) -> Optional[str]:
        index = self.calendar_index.get(date)
        if index is None or index + 1 >= len(self.inputs.calendar):
            return None
        return self.inputs.calendar[index + 1]

    def is_month_last_trading_day(self, date: str) -> bool:
        nxt = self.next_trading_day(date)
        return nxt is not None and nxt[:7] != date[:7]

    def make_event_id(self, signal_date: str, trigger_type: str) -> str:
        self.event_seq += 1
        return f"{signal_date}-{trigger_type}-{self.event_seq:04d}"

    # ── 策略回调 ──

    def run(self, strategy) -> Dict[str, object]:
        """strategy.on_close(engine, date, portfolio) 内部调用 engine.emit() 提交事件。"""
        portfolio = Portfolio(self.fee_rate)
        for date in self.dates:
            self._execute_due(date, portfolio)
            self._record_nav(date, portfolio)
            strategy.on_close(self, date, portfolio)
        self._snapshot_holdings(portfolio)
        return self._write_outputs(strategy)

    def emit(self, event: RebalanceEvent) -> None:
        if event.execution_date is None or not event.targets:
            return
        self.pending.append(event)

    # ── 执行与核算 ──

    def _exec_price(self, ticker: str, date: str) -> Optional[float]:
        price = (self.inputs.close(ticker, date) if self.execution_timing == "close"
                 else self.inputs.open_price(ticker, date))
        # 停牌或执行日价格缺失时不得使用历史收盘价伪造成交。
        return price if price is not None and price > 0 else None

    def _execute_due(self, date: str, portfolio: Portfolio) -> None:
        due = [e for e in self.pending if e.execution_date == date]
        if not due:
            return
        self.pending = [e for e in self.pending if e.execution_date != date]
        # 同日多事件合并：后发事件的目标权重覆盖先发（风险类事件如止损应后发）。
        combined: Dict[str, Tuple[float, RebalanceEvent]] = {}
        for event in due:
            for ticker, weight in event.targets.items():
                combined[ticker] = (weight, event)
        if not combined:
            return

        def price_of(ticker: str) -> Optional[float]:
            return self._exec_price(ticker, date)

        total = portfolio.cash + portfolio.holdings_value(price_of)
        if not math.isfinite(total) or total <= 0:
            return
        for ticker, (target, event) in combined.items():
            old_shares = portfolio.shares.get(ticker, 0.0)
            price = price_of(ticker)
            if price is None or price <= 0:
                continue  # 无任何可用价格：跳过该标的（数据缺失，不伪造成交）。
            old_value = old_shares * price
            old_weight = old_value / total
            if abs(target - old_weight) <= WEIGHT_EPS:
                continue
            desired_value = target * total
            delta_value = desired_value - old_value
            if abs(delta_value) / total <= WEIGHT_EPS:
                continue
            fee = abs(delta_value) * portfolio.fee_rate
            side = "buy" if delta_value > 0 else "sell"
            quantity = abs(delta_value) / price
            if delta_value > 0:
                portfolio.cash -= delta_value + fee
            else:
                portfolio.cash += abs(delta_value) - fee
            portfolio.fee_total += fee
            new_shares = old_shares + delta_value / price
            if new_shares <= 0.0:
                portfolio.shares.pop(ticker, None)
                portfolio.entry_price.pop(ticker, None)
            else:
                portfolio.shares[ticker] = new_shares
                if side == "buy":
                    old_cost = old_shares * portfolio.entry_price.get(ticker, price)
                    added_shares = delta_value / price
                    portfolio.entry_price[ticker] = (old_cost + added_shares * price) / new_shares
            self.record_rows.append({
                "event_id": event.event_id,
                "signal_date": event.signal_date,
                "execution_date": date,
                "trigger_type": event.trigger_type,
                "trigger_reason": event.trigger_reason,
                "ticker": ticker,
                "old_weight": f"{old_weight:.6f}",
                "target_weight": f"{target:.6f}",
                "actual_weight": f"{desired_value / total:.6f}",
            })
            self.trade_rows.append({
                "date": date,
                "ticker": ticker,
                "side": side,
                "quantity": f"{quantity:.4f}",
                "price": f"{price:.6f}",
                "fee": f"{fee:.8f}",
            })

    def _record_nav(self, date: str, portfolio: Portfolio) -> None:
        def price_of(ticker: str) -> Optional[float]:
            return self.inputs.last_close_at_or_before(ticker, date)

        value = portfolio.cash + portfolio.holdings_value(price_of)
        prev = float(self.nav_rows[-1]["nav_value"]) if self.nav_rows else None
        daily_return = 0.0 if not prev else value / prev - 1.0
        self.nav_rows.append({
            "date": date,
            "nav_value": value,
            "strategy_nav": f"{value:.12f}",
            "daily_return": f"{daily_return:.12f}",
        })

    def _snapshot_holdings(self, portfolio: Portfolio) -> None:
        last_date = self.dates[-1] if self.dates else ""
        snapshot: Dict[str, float] = {}
        for ticker, shares in portfolio.shares.items():
            price = self.inputs.last_close_at_or_before(ticker, last_date)
            if price is not None:
                snapshot[ticker] = shares * price
        self.holdings_snapshot = snapshot

    # ── 产物写出 ──

    def _write_outputs(self, strategy) -> Dict[str, object]:
        nav_values = [float(row["nav_value"]) for row in self.nav_rows]
        metrics = compute_metrics(self, nav_values)
        strategy_summary = strategy.describe() if hasattr(strategy, "describe") else {}
        write_json(self.staging / "metrics.json", {"contract": "strategy.metrics.v1", **metrics})
        _write_csv(self.staging / "daily_nav.csv",
                   ["date", "strategy_nav", "daily_return"], self.nav_rows)
        self._write_benchmark_nav()
        self._write_holdings(nav_values)
        _write_csv(self.staging / "trades.csv",
                   ["date", "ticker", "side", "quantity", "price", "fee"], self.trade_rows)
        _write_csv(self.staging / "rebalance_records.csv",
                   ["event_id", "signal_date", "execution_date", "trigger_type", "trigger_reason",
                    "ticker", "old_weight", "target_weight", "actual_weight"], self.record_rows)
        summary = {
            "strategy": strategy_summary,
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
            close = self.inputs.benchmark_close_at_or_before(str(row["date"]))
            if close is not None and base is None:
                base = close
            rows.append({"date": row["date"],
                         "benchmark_nav": f"{close / base:.12f}"
                         if close is not None and base is not None and base > 0 else ""})
        _write_csv(self.staging / "benchmark_nav.csv", ["date", "benchmark_nav"], rows)

    def _write_holdings(self, nav_values: List[float]) -> None:
        date = self.dates[-1] if self.dates else ""
        total = nav_values[-1] if nav_values else 0.0
        rows: List[Dict[str, object]] = []
        if total > 0:
            for ticker in sorted(self.holdings_snapshot):
                rows.append({"ticker": ticker,
                             "weight": f"{self.holdings_snapshot[ticker] / total:.6f}",
                             "as_of": date})
        _write_csv(self.staging / "latest_holdings.csv", ["ticker", "weight", "as_of"], rows)


# ──────────────────────────────────────────────────────────────
# 指标计算
# ──────────────────────────────────────────────────────────────

def compute_metrics(engine: BacktestEngine, nav_values: List[float]) -> Dict[str, object]:
    dates = engine.dates
    first = nav_values[0] if nav_values else 0.0
    last = nav_values[-1] if nav_values else 0.0
    total_return = (last / first - 1.0) if first > 0 and last > 0 else 0.0
    days = max(len(nav_values), 1)
    annualized_return = ((last / first) ** (TRADING_DAYS_PER_YEAR / days) - 1.0) \
        if first > 0 and last > 0 else 0.0
    returns = [nav_values[i] / nav_values[i - 1] - 1.0
               for i in range(1, len(nav_values)) if nav_values[i - 1] > 0]
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        annualized_volatility = math.sqrt(max(variance, 0.0)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    else:
        annualized_volatility = 0.0
    max_drawdown = 0.0
    peak = float("-inf")
    for value in nav_values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    sharpe = (annualized_return / annualized_volatility) if annualized_volatility > 0 else 0.0
    benchmark_total_return = 0.0
    if dates:
        benchmark = engine.inputs.benchmark
        in_range = [d for d in sorted(benchmark) if dates[0] <= d <= dates[-1]]
        if in_range and benchmark[in_range[0]] > 0:
            benchmark_total_return = benchmark[in_range[-1]] / benchmark[in_range[0]] - 1.0
    win_rate = (sum(1 for r in returns if r > 0) / len(returns)) if returns else 0.0
    total_fee = sum(float(row["fee"]) for row in engine.trade_rows)
    return {
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "trading_days": len(nav_values),
        "total_return": _safe(total_return),
        "annualized_return": _safe(annualized_return),
        "annualized_volatility": _safe(annualized_volatility),
        "max_drawdown": _safe(max_drawdown),
        "sharpe": _safe(sharpe),
        "benchmark_total_return": _safe(benchmark_total_return),
        "win_rate": _safe(win_rate),
        "total_fee": _safe(total_fee),
    }


def _safe(value: float) -> float:
    return value if math.isfinite(value) else 0.0


# ──────────────────────────────────────────────────────────────
# CSV / JSON 工具
# ──────────────────────────────────────────────────────────────

def _write_csv(path: Path, headers: List[str], rows: List[Dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
