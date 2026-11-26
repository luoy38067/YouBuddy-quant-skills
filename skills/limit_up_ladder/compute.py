"""limit_up_ladder compute.py（limit.ladder.v1）—— 连板天梯聚合计算器。

名称主源 = 申万行业成员快照（≈5221 成员含名称列，as_of 口径）；md_security 2 片 offset 分页
concat+按代码去重为 best-effort 补充（该表为全资产类型主档，整表分页不可行），末片满 24000 行
时 gaps 提示疑有后续分页；readings 读数块下放可数断言（开盘即刻封板只数、封单额末两位），
LLM 只引用不数表。

runtime=agent_python：
- 取数：app_dq_export 窗口整表导出（涨停明细 30 自然日 / 全市场行情 6 片切片 / 资金流近 4
  自然日窗口 / 封板情绪 / 证券主档名称 2 片分页），不做逐候选查询、不做逐股分批；
- 行业：app_dq_industry_members 按 31 个申万一级行业代码 foreach 取成员快照；
- 计算（Python 确定性规则，LLM 只解释不重算）：
  * 涨停主口径 = LIMIT_TYPE='01' 且 LIMIT_VALUE_CLOSE 非空（收盘封住）；触及未封 = 炸板不入池；
  * ST（SEC_SHORT_NAME 含"ST"）、北交所（代码前缀 4/8/92）不计，计入口径缺口；
  * 连板数 = 从复盘日 D 向前数连续收盘封住的交易日数（含 D），断板即止；
  * 20 日新高 = 30 自然日窗口内（不含 D）收盘最高价被 D 收盘价超越；
  * 封单强度 = LIMIT_VALUE_CLOSE ÷ NEG_MARKET_VALUE；主净 = mkt_equ_mf_new.MAIN_FLOW；
  * min_board（默认 2）过滤入梯，入梯上限 20 只；0 只入梯按"冰点"输出；
  * 涨停价核验 = 实测涨停价 vs 理论涨停价（前收×1.1/1.2）偏差 > 0.006（半个最小变动单位）才
    计数提示——低价股 0.01 tick 舍入致涨幅比 9.6%-10.5% 属正常，不用比例阈值；
  * FIRST_LIMIT_TIME 原始为当日秒数（如 33900=09:25:00），compute 换算 HH:MM:SS。

Usage:
    python compute.py <run_manifest.json>
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

OUTPUT_CONTRACT = "limit.ladder.v1"

SW_L1 = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801110": "家用电器", "801120": "食品饮料", "801130": "纺织服饰",
    "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产", "801200": "商贸零售", "801210": "社会服务", "801230": "综合",
    "801710": "建筑材料", "801720": "建筑装饰", "801730": "电力设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801780": "银行",
    "801790": "非银金融", "801880": "汽车", "801890": "机械设备", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}

LADDER_CAP = 20
LIMIT_TRUNCATE_WARN_ROWS = 23800
UP_PRICE_TICK_TOL = 0.006  # 涨停价核验容差（半个最小变动单位；低价股 tick 舍入可致涨幅 9.6%-10.5%）

LIMIT_REQUIRED = ("ticker_symbol", "trade_date", "limit_value_close")
LIMIT_OPTIONAL = ("limit_type", "limit_price", "first_limit_time", "limit_value_max")
QUOTES_REQUIRED = ("ticker_symbol", "trade_date", "close_price")
QUOTES_OPTIONAL = ("act_pre_close_price", "chg_pct", "turnover_value", "turnover_rate", "neg_market_value")
MF_REQUIRED = ("ticker_symbol", "trade_date", "main_flow")
STATS_REQUIRED = ("trade_date", "up_num")
STATS_OPTIONAL = ("suc_ratio", "con_ratio", "fail_ratio")
MD_REQUIRED = ("ticker_symbol", "sec_name")
DOWN_REQUIRED = ("ticker_symbol", "trade_date", "limit_value_close")

COLUMN_CANDIDATES = {
    "ticker_symbol": ("ticker_symbol", "stock_code", "ticker"),
    "trade_date": ("trade_date",),
    "limit_type": ("limit_type",),
    "limit_price": ("limit_price",),
    "first_limit_time": ("first_limit_time",),
    "limit_value_close": ("limit_value_close",),
    "limit_value_max": ("limit_value_max",),
    "close_price": ("close_price", "close"),
    "act_pre_close_price": ("act_pre_close_price", "pre_close"),
    "chg_pct": ("chg_pct", "change_pct"),
    "turnover_value": ("turnover_value", "turnover_amount"),
    "turnover_rate": ("turnover_rate",),
    "neg_market_value": ("neg_market_value", "neg_mv"),
    "main_flow": ("main_flow",),
    "up_num": ("up_num",),
    "suc_ratio": ("suc_ratio",),
    "con_ratio": ("con_ratio",),
    "fail_ratio": ("fail_ratio",),
    "sec_name": ("sec_name", "sec_short_name", "stock_name", "short_name", "name"),
    "sw_code": ("sw_code",),
}

_FOREACH_INDUSTRY_RE = re.compile(r"industry_(\d{6})$")
_FOREACH_STALE_SECONDS = 120


class ContractError(RuntimeError):
    """Raised when a required artifact violates its contract or is missing."""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Any, digits: int = 2) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _yi(value: Any) -> float | None:
    number = _finite(value)
    return None if number is None else round(number / 1e8, 2)


def _load_manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = path.resolve(strict=True)
    manifest = json.loads(resolved.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 1:
        raise ContractError("仅支持 manifest_version=1")
    return resolved.parent, manifest


def _load_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str)
    frame.columns = [str(col).strip().lower() for col in frame.columns]
    return frame


def _workspace_data_dir(run_dir: Path) -> Path | None:
    """workspace 级 .data：宿主 app_dq_export / foreach_builtin 的原始落盘点。
    run_dir 形如 <workspace>/skill_runs/<skill>/<run_id>。"""
    try:
        if run_dir.parent.parent.name == "skill_runs":
            candidate = run_dir.parent.parent.parent / ".data"
            if candidate.is_dir():
                return candidate
    except OSError:
        pass
    return None


def _load_named(run_dir: Path, run_id: str, suffix: str) -> tuple[pd.DataFrame | None, str | None]:
    """workflow 显式命名落盘通道：workspace/.data/<run_id>_<suffix> 优先（宿主落盘点），
    run 目录内同名文件回退（本地测试布局）。命名确定，不做模糊表头发现。"""
    ws_data = _workspace_data_dir(run_dir)
    if ws_data is not None and run_id:
        path = ws_data / f"{run_id}_{suffix}"
        if path.exists():
            try:
                return _load_csv(path), "named:" + path.name
            except (OSError, ValueError, pd.errors.ParserError):
                return None, None
    for path in sorted(run_dir.rglob(suffix)):
        try:
            return _load_csv(path), "named:" + path.name
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    return None, None


def _load_quotes_slices(run_dir: Path, run_id: str) -> tuple[pd.DataFrame | None, str | None]:
    """行情多片切片通道：workflow 按 5 自然日/片导出 ${run_id}_equd_quotes_p*.csv，
    全市场 30 自然日 ≈11.3 万行超单片上限，必须 concat。"""
    ws_data = _workspace_data_dir(run_dir)
    frames: list[pd.DataFrame] = []
    names: list[str] = []
    if ws_data is not None and run_id:
        for path in sorted(ws_data.glob(f"{run_id}_equd_quotes_p*.csv")):
            try:
                frames.append(_load_csv(path))
                names.append(path.name)
            except (OSError, ValueError, pd.errors.ParserError):
                continue
    if not frames:
        for path in sorted(run_dir.rglob("equd_quotes_p*.csv")):
            try:
                frames.append(_load_csv(path))
                names.append(path.name)
            except (OSError, ValueError, pd.errors.ParserError):
                continue
    if not frames:
        return None, None
    frame = pd.concat(frames, ignore_index=True)
    if {"ticker_symbol", "trade_date"} <= set(frame.columns):
        frame = frame.drop_duplicates(subset=["ticker_symbol", "trade_date"], keep="last")
    return frame, "slices:" + "+".join(names)


def _load_md_security(run_dir: Path, run_id: str) -> tuple[pd.DataFrame | None, str | None, bool]:
    """证券主档分页通道：${run_id}_md_security_p*.csv（offset 24000 分页）concat + 按代码去重；
    无切片时回退单文件 ${run_id}_md_security.csv。
    返回 full_page=True 表示末片满 24000 行（疑有后续分页未取）。"""
    ws_data = _workspace_data_dir(run_dir)
    frames: list[pd.DataFrame] = []
    names: list[str] = []
    full_page = False
    if ws_data is not None and run_id:
        paths = sorted(ws_data.glob(f"{run_id}_md_security_p*.csv"))
        for path in paths:
            try:
                frame = _load_csv(path)
            except (OSError, ValueError, pd.errors.ParserError):
                continue
            frames.append(frame)
            names.append(path.name)
        if frames:
            if len(frames[-1]) >= 24000:
                full_page = True
            merged = pd.concat(frames, ignore_index=True)
            if "ticker_symbol" in merged.columns:
                merged = merged.drop_duplicates(subset=["ticker_symbol"], keep="first")
            return merged, "slices:" + "+".join(names), full_page
        single = ws_data / f"{run_id}_md_security.csv"
        if single.exists():
            try:
                frame = _load_csv(single)
                return frame, "named:" + single.name, len(frame) >= 24000
            except (OSError, ValueError, pd.errors.ParserError):
                return None, None, False
    for path in sorted(run_dir.rglob("md_security*.csv")):
        try:
            frame = _load_csv(path)
            return frame, "named:" + path.name, len(frame) >= 24000
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    return None, None, False


def _fmt_limit_time(value: Any) -> str | None:
    """FIRST_LIMIT_TIME 原始为当日秒数（如 33900=09:25:00）；
    已是 HH:MM:SS 字符串则原样透传，其余原样保留。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    if ":" in text:
        return text
    number = _finite(text)
    if number is not None:
        total = int(number)
        if 0 <= total < 86400:
            return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return text


def _load_foreach_industry_files(
    run_dir: Path, manifest: dict[str, Any]
) -> tuple[pd.DataFrame | None, str | None, list[str]]:
    """聚合 workspace/.data 的 industry_<sw_code>.csv（foreach_builtin 落盘形态，
    无 run_id 前缀、表头 security_id/ticker/name；sw_code 从文件名恢复，
    mtime ≥ manifest created_at 过滤旧 run 遗留）。"""
    ws_data = _workspace_data_dir(run_dir)
    if ws_data is None:
        return None, None, []
    run_start: float | None = None
    created = str(manifest.get("created_at") or "")
    if created:
        try:
            run_start = datetime.fromisoformat(created).timestamp() - _FOREACH_STALE_SECONDS
        except ValueError:
            run_start = None
    frames: list[pd.DataFrame] = []
    used: list[str] = []
    stale: list[str] = []
    for path in sorted(ws_data.glob("industry_*.csv")):
        match = _FOREACH_INDUSTRY_RE.fullmatch(path.stem)
        if not match or match.group(1) not in SW_L1:
            continue
        if run_start is not None:
            try:
                if path.stat().st_mtime < run_start:
                    stale.append(match.group(1))
                    continue
            except OSError:
                continue
        try:
            frame = _load_csv(path)
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        if frame.empty:
            continue
        frame["sw_code"] = match.group(1)
        frames.append(frame)
        used.append(path.name)
    if not frames:
        return None, None, stale
    return pd.concat(frames, ignore_index=True), "foreach:" + "+".join(used), stale


def _resolve_columns(frame: pd.DataFrame, internal_names: tuple[str, ...]) -> tuple[pd.DataFrame, list[str]]:
    rename: dict[str, str] = {}
    for internal in internal_names:
        for cand in COLUMN_CANDIDATES[internal]:
            if cand in frame.columns:
                if cand != internal:
                    rename[cand] = internal
                break
    frame = frame.rename(columns=rename)
    missing = [name for name in internal_names if name not in frame.columns]
    return frame, missing


def _iso_dates(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    frame = frame.copy()
    parsed = pd.to_datetime(frame[column], errors="coerce")
    frame[column] = parsed.dt.strftime("%Y-%m-%d")
    return frame


def _to_num(frame: pd.DataFrame, columns: set[str]) -> pd.DataFrame:
    frame = frame.copy()
    for col in columns:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def _is_bj(ticker: str) -> bool:
    t = (ticker or "").strip()
    return bool(t) and (t[0] in "48" or t.startswith("92"))


def _is_growth(ticker: str) -> bool:
    t = (ticker or "").strip()
    return t.startswith(("300", "301", "688", "689"))


# --------------------------------------------------------------------------
# 主计算
# --------------------------------------------------------------------------

def calculate(manifest_path: Path) -> dict[str, Any]:
    run_dir, manifest = _load_manifest(manifest_path)
    run_id = str(manifest.get("run_id") or "")
    params = manifest.get("parameters") or {}
    requested_date = str(params.get("as_of") or params.get("date") or "") or None
    gaps: list[str] = []
    artifact_names: dict[str, str | None] = {}

    # ---- 涨停明细（必需；30 自然日窗口整表）----
    limit_ind, artifact_names["limit_ind"] = _load_named(run_dir, run_id, "limit_ind.csv")
    if limit_ind is None:
        raise ContractError(
            "找不到涨停明细数据（期望 workspace/.data/<run_id>_limit_ind.csv，"
            "LIMIT_TYPE='01' 窗口整表导出）"
        )
    limit_ind, missing = _resolve_columns(limit_ind, LIMIT_REQUIRED + LIMIT_OPTIONAL)
    if missing:
        raise ContractError(f"涨停明细({artifact_names['limit_ind']})缺少字段: {missing}；实际列: {sorted(limit_ind.columns)}")
    limit_ind = _iso_dates(_to_num(limit_ind, {"limit_value_close", "limit_value_max", "limit_price"}), "trade_date")
    if len(limit_ind) >= LIMIT_TRUNCATE_WARN_ROWS:
        gaps.append(f"涨停明细导出 {len(limit_ind)} 行接近上限，疑有截断，窗口连板计数完整性需人工核对")

    # 复盘日 D = 窗口内最新 TRADE_DATE（公告滞后兜底）
    d_date = str(limit_ind["trade_date"].max())
    if requested_date and requested_date > d_date:
        gaps.append(f"请求日 {requested_date} 尚无涨停数据（公告滞后），已按最新复盘日 {d_date} 出报告")

    limit_day = limit_ind[limit_ind["trade_date"] == d_date]
    sealed_day = limit_day[limit_day["limit_value_close"].notna()]
    bombed_day = limit_day[limit_day["limit_value_close"].isna()]
    pool_all = sorted({str(t).strip() for t in sealed_day["ticker_symbol"]})

    # ---- 行业成员（foreach 落盘通道；行业映射 + 名称主源，as_of 日期口径快照）----
    members, artifact_names["industry_members"], stale_industries = _load_foreach_industry_files(run_dir, manifest)
    ticker_to_sw: dict[str, str] = {}
    member_names: dict[str, str] = {}
    if members is not None:
        members, members_missing = _resolve_columns(members, ("ticker_symbol", "sw_code", "sec_name"))
        if members_missing:
            gaps.append("行业成员数据缺少股票代码或行业列，行业列整体降级为数据不足")
        else:
            for ticker, sw in zip(members["ticker_symbol"].astype(str).str.strip(),
                                  members["sw_code"].astype(str)):
                ticker_to_sw.setdefault(ticker, sw)
            if "sec_name" in members.columns:
                for ticker, name in zip(members["ticker_symbol"].astype(str).str.strip(),
                                        members["sec_name"]):
                    value = None if pd.isna(name) else str(name).strip()
                    if value:
                        member_names.setdefault(ticker, value)
        if stale_industries:
            gaps.append(f"{len(stale_industries)} 个行业的成员文件为本 run 之前遗留已排除"
                        f"（行业: {'、'.join(stale_industries)}），相关行业按数据不足解读")
    else:
        gaps.append("行业成员数据未找到（workspace/.data 无 industry_<sw_code>.csv），行业列整体降级为数据不足")

    # ---- 名称（主源=行业成员快照；md_security 2 片 best-effort 补充——整表 >16.8 万行含全资产类型无法整表分页）----
    md_security, artifact_names["md_security"], md_full_page = _load_md_security(run_dir, run_id)
    md_names: dict[str, str] = {}
    if md_security is not None:
        md_security, md_missing = _resolve_columns(md_security, MD_REQUIRED)
        if not md_missing:
            for ticker, name in zip(md_security["ticker_symbol"].astype(str).str.strip(),
                                    md_security["sec_name"]):
                value = None if pd.isna(name) else str(name).strip()
                if value:
                    md_names.setdefault(ticker, value)
    name_map = {**md_names, **member_names}  # 成员快照优先（as_of 口径；主档分页截断且含多行同代码）
    if md_security is None and members is None:
        gaps.append("证券名称两个来源（行业成员快照/证券主档补充）均未找到，名称列降级为代码、ST 过滤不可用")

    pool: list[str] = []
    st_excluded: list[str] = []
    bj_excluded: list[str] = []
    unknown_name: list[str] = []
    for ticker in pool_all:
        if _is_bj(ticker):
            bj_excluded.append(ticker)
            continue
        name = name_map.get(ticker, "")
        if "ST" in name.upper():
            st_excluded.append(ticker)
            continue
        if not name:
            unknown_name.append(ticker)
        pool.append(ticker)
    if st_excluded:
        gaps.append(f"{len(st_excluded)} 只 ST（简称含 ST）按口径剔除出涨停池：{'、'.join(st_excluded[:6])}{'…' if len(st_excluded) > 6 else ''}")
    if bj_excluded:
        gaps.append(f"{len(bj_excluded)} 只北交所（代码前缀 4/8/92）按口径剔除出涨停池")
    if unknown_name:
        gaps.append(f"{len(unknown_name)} 只个股名称缺失（证券主档与行业成员快照均未命中），无法核验 ST 属性，保留在池中并在此注明：{'、'.join(unknown_name[:8])}{'…' if len(unknown_name) > 8 else ''}")

    # ---- 行情切片（必需；6 片 concat）----
    quotes, artifact_names["quotes"] = _load_quotes_slices(run_dir, run_id)
    quotes_day_by_ticker: dict[str, pd.Series] = {}
    trade_dates: list[str] = sorted({str(d) for d in limit_ind["trade_date"].dropna()})
    if quotes is not None:
        quotes, quotes_missing = _resolve_columns(quotes, QUOTES_REQUIRED + QUOTES_OPTIONAL)
        if any(name in quotes_missing for name in QUOTES_REQUIRED):
            gaps.append(f"行情切片({artifact_names['quotes']})缺少必需列 {quotes_missing}，行情/新高列整体降级为数据不足")
            quotes = None
        else:
            quotes = _iso_dates(_to_num(quotes, {
                "close_price", "act_pre_close_price", "chg_pct",
                "turnover_value", "turnover_rate", "neg_market_value"}), "trade_date")
            trade_dates = sorted(set(trade_dates) | {str(d) for d in quotes["trade_date"].dropna()})
            day_rows = quotes[quotes["trade_date"] == d_date]
            for _, row in day_rows.iterrows():
                t = str(row["ticker_symbol"]).strip()
                if t not in quotes_day_by_ticker:
                    quotes_day_by_ticker[t] = row
            if not day_rows.empty and quotes["trade_date"].max() > d_date:
                gaps.append(f"行情数据最新日 {quotes['trade_date'].max()} 晚于涨停榜面 {d_date}（数据源更新节奏差异），连板计数以榜面为准")
    else:
        gaps.append("行情切片未找到，收盘价/涨跌幅/成交额/换手率/20日新高列整体降级为数据不足")

    dates_desc = sorted(trade_dates, reverse=True)
    try:
        start_idx = dates_desc.index(d_date)
    except ValueError:
        start_idx = 0
    window_dates_desc = dates_desc[start_idx:]

    sealed_mask = limit_ind["limit_value_close"].notna()
    sealed_set = {(str(t).strip(), str(d))
                  for t, d in zip(limit_ind.loc[sealed_mask, "ticker_symbol"],
                                  limit_ind.loc[sealed_mask, "trade_date"])}

    streaks: dict[str, int] = {}
    streak_capped: list[str] = []
    for ticker in pool:
        count = 0
        for date in window_dates_desc:
            if (ticker, date) in sealed_set:
                count += 1
            else:
                break
        streaks[ticker] = count
        if count and count == len(window_dates_desc):
            streak_capped.append(f"{ticker}({count}板)")
    if streak_capped:
        gaps.append(f"窗口起点仍连续封板，连板数为窗口内下界：{'、'.join(streak_capped)}（窗口 30 自然日 ≈20 交易日）")

    # ---- 涨停价核验（对比理论涨停价；不剔除，仅计数）----
    verify_mismatch: list[str] = []
    if "limit_price" in sealed_day.columns:
        for ticker in pool:
            row = quotes_day_by_ticker.get(ticker)
            if row is None:
                continue
            pre_close = _finite(row.get("act_pre_close_price"))
            if not pre_close or pre_close <= 0:
                continue
            lp_rows = sealed_day[sealed_day["ticker_symbol"].astype(str).str.strip() == ticker]
            limit_price_val = _finite(lp_rows["limit_price"].iloc[0]) if not lp_rows.empty else None
            if limit_price_val is None:
                continue
            expected = pre_close * (1.2 if _is_growth(ticker) else 1.1)
            if abs(limit_price_val - expected) > UP_PRICE_TICK_TOL:
                verify_mismatch.append(ticker)
    if verify_mismatch:
        gaps.append(f"{len(verify_mismatch)} 只个股涨停价偏离理论涨停价（前收×1.1/1.2）超半个最小变动单位"
                    f"（{'、'.join(verify_mismatch[:6])}{'…' if len(verify_mismatch) > 6 else ''}），"
                    "可能为异常涨跌幅限制（如 ST 5%），已按名称过滤口径保留，请人工复核")

    min_board = 2
    try:
        min_board = max(1, int(params.get("min_board") or 2))
    except (TypeError, ValueError):
        pass

    ladder_all = [t for t in pool if streaks.get(t, 0) >= min_board]

    # ---- 资金流（可选；窗口导出，取复盘日行，覆盖公告滞后场景）----
    mf, artifact_names["mf"] = _load_named(run_dir, run_id, "mf_day.csv")
    main_flow_map: dict[str, float] = {}
    if mf is not None:
        mf, mf_missing = _resolve_columns(mf, MF_REQUIRED)
        if mf_missing:
            gaps.append(f"资金流数据({artifact_names['mf']})缺少列 {mf_missing}，主净列降级为数据不足")
        else:
            mf = _iso_dates(_to_num(mf, {"main_flow"}), "trade_date")
            if len(mf) >= LIMIT_TRUNCATE_WARN_ROWS:
                gaps.append(f"资金流导出 {len(mf)} 行接近上限，疑有截断（个别标的主净可能缺失）")
            mf_day_rows = mf[mf["trade_date"] == d_date]
            if mf_day_rows.empty:
                gaps.append(f"资金流窗口内无复盘日 {d_date} 的行（共 {len(mf)} 行），主净列整体降级为数据不足")
            else:
                for ticker, flow in zip(mf_day_rows["ticker_symbol"].astype(str).str.strip(),
                                        mf_day_rows["main_flow"]):
                    if pd.notna(flow):
                        main_flow_map.setdefault(ticker, float(flow))
    else:
        gaps.append("资金流数据未找到，主净列整体降级为数据不足")

    # ---- 封板情绪（可选，多日窗口取 D 行）----
    stats, artifact_names["stats"] = _load_named(run_dir, run_id, "limit_stats.csv")
    stats_row: dict[str, float | None] = {"up_num": None, "suc_ratio": None, "con_ratio": None, "fail_ratio": None}
    if stats is not None:
        stats, stats_missing = _resolve_columns(stats, STATS_REQUIRED + STATS_OPTIONAL)
        if stats_missing:
            gaps.append(f"封板情绪数据({artifact_names['stats']})缺少列 {stats_missing}，封板情绪降级为数据不足")
        else:
            stats = _iso_dates(_to_num(stats, {"up_num", "suc_ratio", "con_ratio", "fail_ratio"}), "trade_date")
            row_df = stats[stats["trade_date"] == d_date]
            if row_df.empty:
                row_df = stats[stats["trade_date"] == stats["trade_date"].max()]
                if not row_df.empty:
                    gaps.append(f"封板情绪无复盘日 {d_date} 的行，按最近可用日 {row_df['trade_date'].iloc[0]} 口径")
            if not row_df.empty:
                row = row_df.iloc[0]
                stats_row = {
                    "up_num": _finite(row.get("up_num")),
                    "suc_ratio": _finite(row.get("suc_ratio")),
                    "con_ratio": _finite(row.get("con_ratio")),
                    "fail_ratio": _finite(row.get("fail_ratio")),
                }
    else:
        gaps.append("封板情绪数据未找到（mkt_limit_stats），情绪指标降级为数据不足")
    if stats_row["up_num"] is not None and abs(stats_row["up_num"] - len(pool_all)) > 0.5:
        gaps.append(f"mkt_limit_stats.UP_NUM={int(stats_row['up_num'])} 与收盘封住家数 {len(pool_all)} 不一致"
                    "（UP_NUM 或含 ST/北交所/口径差异），按事实分列，不强行归一")

    # ---- 跌停明细（可选；空/缺失降级）----
    down_ind, artifact_names["limit_down"] = _load_named(run_dir, run_id, "limit_ind_down.csv")
    limit_down_count: int | None = None
    if down_ind is not None:
        down_ind, down_missing = _resolve_columns(down_ind, DOWN_REQUIRED)
        if down_missing:
            gaps.append(f"跌停明细({artifact_names['limit_down']})缺少列 {down_missing}，跌停家数降级为数据不足")
        else:
            down_ind = _iso_dates(_to_num(down_ind, {"limit_value_close"}), "trade_date")
            if down_ind.empty:
                gaps.append("跌停明细导出为空（LIMIT_TYPE='02' 可能为不存在的类型编码），跌停家数按数据不足处理")
            else:
                down_sealed = down_ind[(down_ind["trade_date"] == d_date) & down_ind["limit_value_close"].notna()]
                limit_down_count = int(len(down_sealed))
    else:
        gaps.append("跌停明细未导出，跌停家数按数据不足处理")

    def _industry(ticker: str) -> str | None:
        if _is_bj(ticker):
            return None
        sw = ticker_to_sw.get(ticker)
        return SW_L1.get(sw) if sw else None

    def _quote_field(ticker: str, field: str) -> Any:
        row = quotes_day_by_ticker.get(ticker)
        if row is None:
            return None
        value = row.get(field)
        return None if value is None or pd.isna(value) else float(value)

    # ---- 20 日新高（30 自然日窗口，不含 D）----
    prev_high: dict[str, float] = {}
    if quotes is not None:
        hist = quotes[quotes["trade_date"] < d_date]
        if not hist.empty:
            grouped = hist.groupby(hist["ticker_symbol"].astype(str).str.strip())["close_price"].max()
            prev_high = {str(t): float(v) for t, v in grouped.items() if pd.notna(v)}

    # ---- 组装入梯行（连板降序，组内收盘封单额降序，缺失按成交额降序）----
    seal_map: dict[str, float] = {}
    time_map: dict[str, str] = {}
    for _, row in sealed_day.iterrows():
        t = str(row["ticker_symbol"]).strip()
        val = row.get("limit_value_close")
        if pd.notna(val) and t not in seal_map:
            seal_map[t] = float(val)
        ft = row.get("first_limit_time")
        if pd.notna(ft) and str(ft).strip() and t not in time_map:
            time_map[t] = _fmt_limit_time(ft)

    def _ladder_sort_key(ticker: str) -> tuple:
        seal = seal_map.get(ticker)
        turnover = _quote_field(ticker, "turnover_value")
        return (-streaks.get(ticker, 0),
                -(seal if seal is not None else (turnover if turnover is not None else 0.0)))

    ladder_sorted = sorted(ladder_all, key=_ladder_sort_key)
    ladder_cut = ladder_sorted[:LADDER_CAP]
    if len(ladder_sorted) > LADDER_CAP:
        gaps.append(f"入梯个股 {len(ladder_sorted)} 只超上限，按连板数、收盘封单额降序仅展示前 {LADDER_CAP} 只")

    ladder_rows = []
    for ticker in ladder_cut:
        name = name_map.get(ticker) or member_names.get(ticker)
        close = _quote_field(ticker, "close_price")
        prev = prev_high.get(ticker)
        if prev is None:
            new_high = None
        else:
            new_high = (close is not None and close > prev)
        seal = seal_map.get(ticker)
        neg_mv = _quote_field(ticker, "neg_market_value")
        seal_strength = (seal / neg_mv) if (seal is not None and neg_mv) else None
        ladder_rows.append({
            "board": streaks.get(ticker, 0),
            "ticker": ticker,
            "name": name,
            "industry": _industry(ticker),
            "close_price": _round(close, 3),
            "chg_pct": _round(_quote_field(ticker, "chg_pct"), 4),
            "first_limit_time": time_map.get(ticker),
            "limit_value_close": _round(seal, 0),
            "limit_value_close_yi": _yi(seal),
            "limit_value_max": _round(_finite(_first_optional(sealed_day, ticker, "limit_value_max")), 0),
            "limit_value_max_yi": _yi(_first_optional(sealed_day, ticker, "limit_value_max")),
            "seal_strength": _round(seal_strength, 4),
            "main_flow": _round(main_flow_map.get(ticker), 0),
            "main_flow_yi": _yi(main_flow_map.get(ticker)),
            "turnover_value": _quote_field(ticker, "turnover_value"),
            "turnover_rate": _round(_quote_field(ticker, "turnover_rate"), 4),
            "is_new_high_20d": new_high,
            "is_bj": False,
        })

    # ---- 梯队结构（按全部入梯，未截断前）----
    boards = [streaks.get(t, 0) for t in ladder_all]
    structure = {
        "total_count": len(ladder_all),
        "max_board": max(boards) if boards else 0,
        "high_count": sum(1 for b in boards if b >= 5),
        "mid_count": sum(1 for b in boards if 3 <= b <= 4),
        "low_count": sum(1 for b in boards if b == 2),
    }
    if structure["total_count"] == 0:
        gaps.append("无连板个股入梯（min_board=%s），按'冰点'解读，本报告仅输出涨停池与情绪概览" % min_board)

    # ---- 行业聚合（基于展示梯队）----
    industry_groups: dict[str, list[dict[str, Any]]] = {}
    for row in ladder_rows:
        key = row["industry"] or "未分类"
        industry_groups.setdefault(key, []).append(row)
    industry_summary = []
    for industry, rows in industry_groups.items():
        best = max(rows, key=lambda r: (r["board"], r["limit_value_close"] or 0))
        industry_summary.append({
            "industry": industry,
            "count": len(rows),
            "max_board": best["board"],
            "representative_ticker": best["ticker"],
            "representative_name": best["name"],
        })
    industry_summary.sort(key=lambda g: (-g["count"], -g["max_board"]))

    # ---- 读数辅助（可数断言由 Python 给出，LLM 只引用不数表）----
    open_seal_count = sum(
        1 for r in ladder_rows
        if r.get("first_limit_time") and str(r["first_limit_time"]) <= "09:25:59")
    bottom2 = sorted(
        (r for r in ladder_rows if r.get("limit_value_close") is not None),
        key=lambda r: r["limit_value_close"])[:2]
    readings = {
        "open_seal_count": int(open_seal_count),
        "seal_amount_bottom2": [
            {"ticker": r["ticker"], "name": r["name"], "seal_amount_yi": r.get("limit_value_close_yi")}
            for r in bottom2
        ],
    }

    return {
        "ok": True,
        "contract": OUTPUT_CONTRACT,
        "as_of": d_date,
        "requested_date": requested_date,
        "min_board": min_board,
        "sentiment": {
            "limit_up_count": int(stats_row["up_num"]) if stats_row["up_num"] is not None else None,
            "pool_size_all": len(pool_all),
            "pool_size": len(pool),
            "limit_down_count": limit_down_count,
            "suc_ratio": _round(stats_row["suc_ratio"], 4),
            "con_ratio": _round(stats_row["con_ratio"], 4),
            "fail_ratio": _round(stats_row["fail_ratio"], 4),
            "bombed_count": int(len(bombed_day)),
        },
        "structure": structure,
        "ladder": ladder_rows,
        "industry_summary": industry_summary,
        "industry_basis": "申万一级行业成员快照（app_dq_industry_members foreach 31 码），题材近似口径；北交所无申万成分映射",
        "readings": readings,
        "data_quality": {
            "limit_rows_total": len(limit_ind),
            "limit_rows_day": int(len(limit_day)),
            "pool_size_all": len(pool_all),
            "pool_size": len(pool),
            "st_excluded": len(st_excluded),
            "bj_excluded": len(bj_excluded),
            "bombed_count": int(len(bombed_day)),
            "quotes_rows": int(len(quotes)) if quotes is not None else 0,
            "quote_dates": len(dates_desc),
            "quote_window": f"{min(trade_dates)}..{max(trade_dates)}" if trade_dates else None,
            "mf_rows": int(len(mf)) if mf is not None else 0,
            "stats_rows": int(len(stats)) if stats is not None else 0,
            "md_rows": int(len(md_security)) if md_security is not None else 0,
            "members_industries": len(set(ticker_to_sw.values())),
            "verify_mismatch": len(verify_mismatch),
            "artifact_names": artifact_names,
        },
        "gaps": gaps,
        "error": None,
    }


def _first_optional(frame: pd.DataFrame, ticker: str, column: str) -> float | None:
    if column not in frame.columns:
        return None
    rows = frame[frame["ticker_symbol"].astype(str).str.strip() == ticker]
    if rows.empty:
        return None
    return _finite(rows[column].iloc[0])


def check_output(result: dict[str, Any]) -> None:
    if result.get("ok") and not result.get("ladder") and not result.get("gaps"):
        raise ValueError("输出缺少梯队与缺口说明")
    if result.get("contract") != OUTPUT_CONTRACT:
        raise ValueError("输出契约不符")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    manifest_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("run_manifest.json")
    try:
        result = calculate(manifest_path)
        check_output(result)
    except ContractError as exc:
        result = {"ok": False, "contract": OUTPUT_CONTRACT, "as_of": None,
                  "structure": None, "ladder": [], "industry_summary": [], "gaps": [str(exc)],
                  "error": {"code": "COMPUTE_FAILED", "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "contract": OUTPUT_CONTRACT, "as_of": None,
                  "structure": None, "ladder": [], "industry_summary": [], "gaps": [f"计算失败: {exc}"],
                  "error": {"code": "COMPUTE_FAILED", "message": str(exc)}}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
