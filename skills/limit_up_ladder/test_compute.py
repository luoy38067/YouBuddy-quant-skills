"""limit_up_ladder compute.py 本地合成数据测试。

不依赖宿主：在临时目录构造 workspace 布局（skill_runs/limit_up_ladder/<run_id> + workspace/.data），
按宿主落盘形态喂 CSV（${run_id}_ 显式命名导出、equd_quotes_p* 多片切片、
md_security_p* 分页切片、industry_<sw_code>.csv foreach 形态），断言涨停池判定、ST/北交所剔除、
连板计数、20 日新高、封单强度、涨停时间秒数换算、涨停价容差核验、资金流复盘日行过滤、
封板情绪、跌停计数、行业聚合与降级/失败路径。

Run: PYTHONIOENCODING=utf-8 python test_compute.py
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compute  # noqa: E402

D = "2026-11-03"

LIMIT_HEADER = ["TICKER_SYMBOL", "TRADE_DATE", "LIMIT_TYPE", "LIMIT_PRICE",
                "FIRST_LIMIT_TIME", "LIMIT_VALUE_CLOSE", "LIMIT_VALUE_MAX"]


def _limit_rows() -> list:
    rows = []
    for date in ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-27",
                 "2026-10-28", "2026-10-29", "2026-11-02", D]:
        close, mx = ("210000000", "250000000") if date == D else ("150000000", "180000000")
        rows.append(["300100", date, "01", 14.40, 33900, close, mx])   # 8 连板（窗口下界）；秒数=09:25:00
    for date in ["2026-10-29", "2026-11-02", D]:
        rows.append(["600001", date, "01", 10.78, 34260, 80000000, 100000000])  # 3 板；10.78=9.8×1.1 理论涨停价（秒数=09:31:00）
    for date in ["2026-11-02", D]:
        rows.append(["600007", date, "01", 5.17, "14:50:00", 30000000, 35000000])    # 2 板
    rows.append(["000002", D, "01", 8.20, "09:25:00", 50000000, 60000000])  # 1 板（不入梯）
    rows.append(["000004", D, "01", 4.95, "10:00:00", 40000000, 45000000])  # 1 板
    rows.append(["920001", D, "01", 3.62, "09:30:00", 20000000, 22000000])  # 北交所 → 剔除
    rows.append(["600005", D, "01", 5.94, "09:25:00", 30000000, 33000000])  # ST → 剔除
    rows.append(["000006", D, "01", 4.40, "13:45:00", "", ""])              # 炸板 → 不入池
    return rows


QUOTE_HEADER = ["TICKER_SYMBOL", "TRADE_DATE", "CLOSE_PRICE", "ACT_PRE_CLOSE_PRICE",
                "CHG_PCT", "TURNOVER_VALUE", "TURNOVER_RATE", "NEG_MARKET_VALUE"]

QUOTES_P1 = [
    ["300100", "2026-11-02", 12.0, 10.91, 0.0999, 500000000, 0.10, 2000000000],
    ["300100", D, 12.5, 12.0, 0.0413, 550000000, 0.11, 2100000000],
    ["600001", "2026-11-02", 9.8, 8.91, 0.0999, 70000000, 0.05, 10000000000],
    ["600001", D, 10.2, 9.8, 0.0408, 80000000, 0.055, 10200000000],
    ["600007", "2026-11-02", 4.7, 4.27, 0.1007, 25000000, 0.03, 800000000],
    ["600007", D, 4.8, 4.7, 0.0213, 30000000, 0.035, 820000000],
    ["000002", D, 8.2, 7.45, 0.1007, 40000000, 0.06, 900000000],
    ["000004", D, 4.95, 4.5, 0.10, 20000000, 0.04, 500000000],
    ["920001", D, 3.62, 2.78, 0.302, 15000000, 0.08, 400000000],
    ["600005", D, 5.94, 5.4, 0.10, 12000000, 0.03, 300000000],
    ["000006", D, 4.41, 4.0, 0.1025, 18000000, 0.03, 600000000],
]

QUOTES_P2 = [
    ["300100", "2026-10-20", 10.0, 9.1, 0.0989, 400000000, 0.09, 1800000000],
    ["300100", "2026-10-21", 10.3, 10.0, 0.03, 420000000, 0.09, 1850000000],
    ["300100", "2026-10-22", 10.6, 10.3, 0.0291, 430000000, 0.09, 1900000000],
    ["300100", "2026-10-27", 10.9, 10.6, 0.0283, 440000000, 0.09, 1950000000],
    ["300100", "2026-10-28", 11.2, 10.9, 0.0275, 450000000, 0.10, 1980000000],
    ["300100", "2026-10-29", 11.5, 11.2, 0.0268, 480000000, 0.10, 2000000000],
    ["600001", "2026-10-29", 9.5, 8.65, 0.0983, 60000000, 0.04, 9800000000],
    ["600007", "2026-10-29", 5.0, 4.55, 0.0989, 22000000, 0.03, 780000000],
]

MD_ROWS = [["600001", "优选一"], ["300100", "创业百"], ["000002", "流出股"], ["000004", "双录股"],
           ["600007", "七喜"], ["600005", "ST中安"], ["920001", "北交一"], ["000006", "炸板股"],
           ["600099", "跌停股"], ["600098", "触跌股"]]

MF_ROWS = [["600001", D, 50000000], ["300100", D, -20000000],
           ["300100", "2026-11-02", 111111], ["600007", "2026-11-02", 999999]]  # 后两行=非复盘日诱饵，D 行过滤回归

STATS_HEADER = ["TRADE_DATE", "UP_NUM", "SUC_RATIO", "CON_RATIO", "FAIL_RATIO"]
STATS_ROWS = [["2026-11-02", 4, 0.75, 0.25, 0.25], [D, 7, 0.8, 0.3, 0.2]]

DOWN_HEADER = ["TICKER_SYMBOL", "TRADE_DATE", "LIMIT_TYPE", "LIMIT_PRICE", "LIMIT_VALUE_CLOSE"]
DOWN_ROWS = [["600099", D, "02", 3.24, 5000000], ["600098", D, "02", 3.42, ""]]

MEMBERS = {"801010": [("600001", "优选一")], "801080": [("300100", "创业百")],
           "801730": [("600007", "七喜")]}


def _write_csv(path: Path, header: list[str], rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _write_foreach(ws_data: Path, sw_code: str, members: list[tuple[str, str]]) -> None:
    rows = [[f"sec{i}", ticker, name] for i, (ticker, name) in enumerate(members)]
    _write_csv(ws_data / f"industry_{sw_code}.csv", ["security_id", "ticker", "name"], rows)


def _build_run(base: Path, *, with_down=True, with_stats=True, with_mf=True,
               min_board: int = 2, md_slices=True, md_full_last=False,
               md_skip: str | None = None, members_skip: str | None = None) -> Path:
    md_rows = [r for r in MD_ROWS if r[0] != md_skip] if md_skip else MD_ROWS
    members_eff = ({sw: [m for m in ms if m[0] != members_skip]
                    for sw, ms in MEMBERS.items()} if members_skip else MEMBERS)
    run_dir = base / "skill_runs" / "limit_up_ladder" / "r1"
    ws_data = base / ".data"
    _write_csv(ws_data / "r1_limit_ind.csv", LIMIT_HEADER, _limit_rows())
    if with_down:
        _write_csv(ws_data / "r1_limit_ind_down.csv", DOWN_HEADER, DOWN_ROWS)
    _write_csv(ws_data / "r1_equd_quotes_p1.csv", QUOTE_HEADER, QUOTES_P1)
    _write_csv(ws_data / "r1_equd_quotes_p2.csv", QUOTE_HEADER, QUOTES_P2)
    if with_mf:
        _write_csv(ws_data / "r1_mf_day.csv", ["TICKER_SYMBOL", "TRADE_DATE", "MAIN_FLOW"], MF_ROWS)
    if with_stats:
        _write_csv(ws_data / "r1_limit_stats.csv", STATS_HEADER, STATS_ROWS)
    if md_slices and not md_full_last:
        _write_csv(ws_data / "r1_md_security_p1.csv", ["TICKER_SYMBOL", "SEC_SHORT_NAME"], md_rows[:4])
        _write_csv(ws_data / "r1_md_security_p2.csv", ["TICKER_SYMBOL", "SEC_SHORT_NAME"], md_rows[4:7])
        _write_csv(ws_data / "r1_md_security_p3.csv", ["TICKER_SYMBOL", "SEC_SHORT_NAME"], md_rows[7:])
    elif md_slices and md_full_last:
        # 末片恰满 24000 行 → compute 应提示疑有后续分页
        _write_csv(ws_data / "r1_md_security_p1.csv", ["TICKER_SYMBOL", "SEC_SHORT_NAME"], md_rows)
        filler = [[f"F{i:05d}", f"填充{i:05d}"] for i in range(24000)]
        _write_csv(ws_data / "r1_md_security_p2.csv", ["TICKER_SYMBOL", "SEC_SHORT_NAME"], filler)
    else:
        _write_csv(ws_data / "r1_md_security.csv", ["TICKER_SYMBOL", "SEC_SHORT_NAME"], md_rows)
    for sw, members in members_eff.items():
        _write_foreach(ws_data, sw, members)
    _write_foreach(ws_data, "801020", [("800001", "非法行业代码诱饵")])
    stale = ws_data / "industry_801230.csv"
    _write_foreach(ws_data, "801230", [("800002", "过期行业诱饵")])
    old = datetime.now().timestamp() - 3600
    os.utime(stale, (old, old))
    manifest = {
        "manifest_version": 1,
        "run_id": "r1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parameters": {"as_of": D, "min_board": min_board},
        "artifacts": {},
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _run_cli(manifest_path: Path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "compute.py", str(manifest_path)],
                          cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=120)


def _variant_full() -> None:
    base = Path(tempfile.mkdtemp(prefix="ladder_full_"))
    manifest_path = _build_run(base)
    result = compute.calculate(manifest_path)

    assert result["ok"] is True, result
    assert result["contract"] == "limit.ladder.v1"
    assert result["as_of"] == D

    sentiment = result["sentiment"]
    assert sentiment["limit_up_count"] == 7
    assert sentiment["pool_size_all"] == 7
    assert sentiment["pool_size"] == 5
    assert sentiment["limit_down_count"] == 1
    assert sentiment["suc_ratio"] == 0.8 and sentiment["con_ratio"] == 0.3 and sentiment["fail_ratio"] == 0.2
    assert sentiment["bombed_count"] == 1

    structure = result["structure"]
    assert structure == {"total_count": 3, "max_board": 8, "high_count": 1, "mid_count": 1, "low_count": 1}

    ladder = result["ladder"]
    assert [row["ticker"] for row in ladder] == ["300100", "600001", "600007"]
    assert [row["board"] for row in ladder] == [8, 3, 2]
    top = ladder[0]
    assert top["name"] == "创业百" and top["industry"] == "电子"
    assert top["close_price"] == 12.5 and top["chg_pct"] == 0.0413
    assert top["first_limit_time"] == "09:25:00"     # 33900 秒换算
    assert top["limit_value_close"] == 210000000.0 and top["limit_value_close_yi"] == 2.1
    assert top["limit_value_max_yi"] == 2.5
    assert top["seal_strength"] == 0.1
    assert top["main_flow_yi"] == -0.2               # D 行 -20000000，非复盘日诱饵 111111 不入
    assert top["is_new_high_20d"] is True
    second = ladder[1]
    assert second["name"] == "优选一" and second["industry"] == "农林牧渔"
    assert second["first_limit_time"] == "09:31:00"  # 34260 秒换算
    assert second["seal_strength"] == 0.0078 and second["main_flow_yi"] == 0.5
    assert second["is_new_high_20d"] is True
    third = ladder[2]
    assert third["name"] == "七喜" and third["industry"] == "电力设备"
    assert third["first_limit_time"] == "14:50:00"   # 已是时间字符串，原样透传
    assert third["main_flow"] is None                # 只有非复盘日诱饵行 → D 过滤后数据不足
    assert third["is_new_high_20d"] is False   # 窗口前高 5.0 > D 收盘 4.8

    summary = result["industry_summary"]
    assert [g["industry"] for g in summary] == ["电子", "农林牧渔", "电力设备"]
    assert summary[0]["max_board"] == 8 and summary[0]["representative_name"] == "创业百"

    readings = result["readings"]
    assert readings["open_seal_count"] == 1   # 仅 300100（09:25:00 ≤ 09:25:59）；600001 09:31 / 600007 14:50 不算
    assert [b["ticker"] for b in readings["seal_amount_bottom2"]] == ["600007", "600001"]  # 3000 万 < 8000 万 < 2.1 亿

    dq = result["data_quality"]
    assert dq["st_excluded"] == 1 and dq["bj_excluded"] == 1
    assert dq["quotes_rows"] == 19 and dq["quote_dates"] == 8
    assert dq["verify_mismatch"] == 0
    assert dq["members_industries"] == 3
    assert dq["artifact_names"]["limit_ind"] == "named:r1_limit_ind.csv"
    assert str(dq["artifact_names"]["quotes"]).startswith("slices:")
    assert dq["artifact_names"]["stats"] == "named:r1_limit_stats.csv"
    assert dq["artifact_names"]["md_security"] == \
        "slices:r1_md_security_p1.csv+r1_md_security_p2.csv+r1_md_security_p3.csv"
    assert str(dq["artifact_names"]["industry_members"]).startswith("foreach:")

    joined = "\n".join(result["gaps"])
    assert "窗口起点仍连续封板" in joined and "300100(8板)" in joined
    assert "ST（简称含 ST）按口径剔除" in joined
    assert "北交所" in joined
    assert "公告滞后" not in joined
    assert "不一致" not in joined          # UP_NUM 7 = pool_all 7
    assert "接近上限" not in joined

    proc = _run_cli(manifest_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cli_out = json.loads(proc.stdout)
    assert cli_out["ok"] is True and cli_out["structure"]["max_board"] == 8
    print("PASS 变体1 完整链路（多片行情/连板计数/ST·北交所剔除/新高/封单强度/情绪/跌停/行业聚合/CLI exit 0）")


def _variant_optional_missing() -> None:
    base = Path(tempfile.mkdtemp(prefix="ladder_opt_"))
    manifest_path = _build_run(base, with_down=False, with_stats=False, with_mf=False)
    result = compute.calculate(manifest_path)
    assert result["ok"] is True
    assert result["sentiment"]["limit_up_count"] is None
    assert result["sentiment"]["limit_down_count"] is None
    assert result["sentiment"]["pool_size"] == 5 and result["sentiment"]["bombed_count"] == 1
    assert result["ladder"][0]["main_flow"] is None
    assert result["structure"]["max_board"] == 8
    joined = "\n".join(result["gaps"])
    assert "跌停明细未导出" in joined
    assert "封板情绪数据未找到" in joined
    assert "资金流数据未找到" in joined
    print("PASS 变体2 可选表缺失降级（情绪/跌停/主净 null，池与梯队不受影响）")


def _variant_min_board3() -> None:
    base = Path(tempfile.mkdtemp(prefix="ladder_mb3_"))
    manifest_path = _build_run(base, min_board=3, md_slices=False)
    result = compute.calculate(manifest_path)
    assert result["ok"] is True
    assert [row["ticker"] for row in result["ladder"]] == ["300100", "600001"]
    assert result["structure"]["total_count"] == 2
    assert result["structure"]["low_count"] == 0 and result["structure"]["mid_count"] == 1
    # 单文件 md_security 通道回退兼容
    assert result["data_quality"]["artifact_names"]["md_security"] == "named:r1_md_security.csv"
    print("PASS 变体3 min_board=3 过滤（1 板股不入梯，结构计数同步；单文件 md_security 回退兼容）")


def _variant_failure() -> None:
    base = Path(tempfile.mkdtemp(prefix="ladder_fail_"))
    run_dir = base / "skill_runs" / "limit_up_ladder" / "r2"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps({
        "manifest_version": 1, "run_id": "r2",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "parameters": {}, "artifacts": {},
    }), encoding="utf-8")
    try:
        compute.calculate(manifest_path)
        raise AssertionError("缺涨停明细时应抛 ContractError")
    except compute.ContractError as exc:
        assert "涨停明细" in str(exc)
    proc = _run_cli(manifest_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    cli_out = json.loads(proc.stdout)
    assert cli_out["ok"] is False and cli_out["error"]["code"] == "COMPUTE_FAILED"
    print("PASS 变体4 涨停明细缺失失败路径（ContractError + CLI exit 1 + stdout 诊断 JSON）")


def _variant_md_full_page() -> None:
    base = Path(tempfile.mkdtemp(prefix="ladder_mdfull_"))
    manifest_path = _build_run(base, md_full_last=True)
    result = compute.calculate(manifest_path)
    assert result["ok"] is True
    joined = "\n".join(result["gaps"])
    # md_security 为 best-effort 补充源，末片满 24000 不再触发 gap（名称主源=成员快照兜底）
    assert "末片满 24000" not in joined and "名称缺失" not in joined, joined
    assert result["ladder"][2]["name"] == "七喜"
    assert result["data_quality"]["st_excluded"] == 1
    print("PASS 变体5 md_security 末片满 24000（补充源 best-effort）→ 无缺失 gap，成员快照兜底")


def _variant_member_name_primary() -> None:
    """名称主源=行业成员快照：md_security 缺 600007，成员快照有 → 名称照常、无缺失 gap。"""
    base = Path(tempfile.mkdtemp(prefix="ladder_mnp_"))
    manifest_path = _build_run(base, md_skip="600007")
    result = compute.calculate(manifest_path)
    assert result["ok"] is True
    assert result["ladder"][2]["name"] == "七喜"   # 成员快照名称兜底
    joined = "\n".join(result["gaps"])
    assert "名称缺失" not in joined, joined
    assert result["data_quality"]["st_excluded"] == 1
    print("PASS 变体6 名称主源=成员快照（主档缺 600007 时由成员快照补名，无缺失 gap）")


def _variant_name_missing() -> None:
    """两源均未命中 → 名称缺失 gap 点名代码。"""
    base = Path(tempfile.mkdtemp(prefix="ladder_nm_"))
    manifest_path = _build_run(base, md_skip="600007", members_skip="600007")
    result = compute.calculate(manifest_path)
    assert result["ok"] is True
    joined = "\n".join(result["gaps"])
    assert "1 只个股名称缺失" in joined and "600007" in joined, joined
    assert result["data_quality"]["st_excluded"] == 1
    print("PASS 变体7 双源均缺 → 名称缺失 gap 点名代码（列出 ticker）")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    _variant_full()
    _variant_optional_missing()
    _variant_min_board3()
    _variant_failure()
    _variant_md_full_page()
    _variant_member_name_primary()
    _variant_name_missing()
    print("全部 7 个变体通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
