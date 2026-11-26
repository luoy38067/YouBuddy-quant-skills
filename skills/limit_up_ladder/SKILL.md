---
name: limit_up_ladder
flow_version: 1
inputs:
  as_of:
    type: string
    required: false
    description: 期望复盘交易日（YYYY-MM-DD），缺省为今天；公告滞后时自动回退最新有数据日
datasets:
  cross_section:
    contract: market.cross_section.v1
    producer: app_dq_export
    source: tl_stock_quote.mkt_equd
    required: false
output:
  format: markdown
  template: report.md
---

# 连板天梯（复盘）

对复盘日 D 做连板梯队复盘：涨停池识别、连板计数与梯队分组（收盘价、涨停时间、封单额、峰封、
封单强度、主净、成交额、换手率、20 日新高、行业）、行业聚合与全市场封板情绪概览，按 `report.md` 输出。
**全部判定与计数为 Python 精确计算（窗口整表导出 + 本地 join），不做逐候选查询、逐股分批。**

本 Skill 使用宿主固定编排，不由 LLM 生成取数参数。

1. 宿主按 `workflow.json` 机械执行（各步骤 label 带 `[v0.3.5]` 版本标记，便于核对加载版本）：
   - `app_dq_index_constituents_by_name` 取**创业板成分**（第 1 步）——宿主编排前置步骤，
     与连板天梯无关，compute 不消费；
   - `app_dq_export` 导出 `mkt_limit_ind`（`LIMIT_TYPE='01'`，30 自然日窗口整表 ≈20 交易日，
     约 3300 行，limit 24000）；连板计数在本地做；
   - `app_dq_export` 导出 `mkt_limit_ind` `LIMIT_TYPE='02'`（跌停；空结果/缺失降级为
     "数据不足"，不阻塞）；
   - `app_dq_export` 导出 `mkt_equd` 全市场行情 **6 片切片**（30 自然日 ≈11.3 万行超单片 24000
     上限，5 自然日/片 ≈2.26 万行，`${run_id}_equd_quotes_p1..p6.csv`）；
   - `app_dq_export` 导出 `mkt_equ_mf_new` 资金流**近 4 自然日窗口**（≈2 万行，覆盖公告滞后
     场景；compute 取窗口内复盘日行）；
   - `app_dq_export` 导出 `mkt_limit_stats`（近 7 自然日窗口），compute 取复盘日行；
   - `app_dq_export` 导出 `tl_main.md_security` 名称 **2 片 best-effort 补充**（offset 0/24000，
     该表为全资产类型主档，无法整表分页；越界分页返回空行为正常）。**名称主源 = 申万行业成员快照**：
     成员快照（as_of 口径）优先、主档补充，两源均未命中才记名称缺失并点名代码；
   - `app_dq_industry_members` 按 **31 个申万一级行业代码** foreach 逐行业取成员
     （**行业映射 + 名称主源**：≈5221 成员含名称列，as_of 口径快照）；
   - 执行 `compute_limit_ladder` 并校验输出 schema（`limit.ladder.v1`）。
2. **artifact 读取通道**：workflow 显式命名落盘——workspace 级 `.data/` 的
   `${run_id}_limit_ind.csv` / `${run_id}_limit_ind_down.csv` / `${run_id}_equd_quotes_p*.csv`
   （多片 concat，按 (ticker, date) 去重）/ `${run_id}_mf_day.csv` / `${run_id}_limit_stats.csv` /
   `${run_id}_md_security_p*.csv`（2 片补充 concat，按代码去重；无切片时回退单文件
   `${run_id}_md_security.csv`）；行业成员走 foreach 落盘通道
   （`.data/industry_<sw_code>.csv`，无 run_id 前缀，mtime 早于本 run created_at 的旧文件排除）。
3. Python 输出是最终数字的权威来源；LLM 不得自行重算冲突数字。
4. 收到 `completion_contract.no_more_tools=true` 后禁止继续调用任何工具。
5. 数据不完整时输出失败诊断，不得生成假装完整的报告。

## 一、Python 已按确定性规则给出（LLM 只解释，不重算）

1. **复盘日**（as_of）：窗口内涨停明细最新 TRADE_DATE；请求日晚于它时 gaps 注明"公告滞后"。
2. **涨停池**：`LIMIT_TYPE='01'` 且 `LIMIT_VALUE_CLOSE` 非空 = 收盘封住（入池）；触及未封 =
   炸板（bombed_count，不入池）。**ST（SEC_SHORT_NAME 含"ST"）与北交所（代码前缀 4/8/92）
   按口径剔除**，剔除家数在 gaps 注明；名称缺失股保留并注明（ST 过滤不可用时的降级）。
3. **连板数**：从 D 向前数连续收盘封住的交易日数（含 D），断板即止；窗口起点仍封板的记为
   下界并在 gaps 注明。`min_board`（默认 2）过滤入梯；0 只入梯 = "冰点"，仅输出池与情绪。
4. **入梯行**（ladder）：连板降序、组内收盘封单额（缺失按成交额）降序，上限 20 只（截断在
   gaps 注明）。封单强度 = 封单额 ÷ 流通市值；主净 = 资金流窗口内复盘日行的 MAIN_FLOW；
   20 日新高 = 30 自然日窗口（不含 D）收盘最高价被 D 收盘超越（窗口不足时为 null）。
   涨停时间：FIRST_LIMIT_TIME 原始为当日秒数（如 33900=09:25:00），compute 换算为
   HH:MM:SS（已是时间字符串则原样透传）。
5. **封板情绪**（sentiment）：mkt_limit_stats 复盘日行的 UP_NUM/SUC_RATIO/CON_RATIO/FAIL_RATIO；
   UP_NUM 与收盘封住家数分列如实对照（口径差异不强行归一）；跌停家数来自 `LIMIT_TYPE='02'`
   收盘封住计数（缺失/空 → 数据不足）。
6. **行业聚合**（industry_summary）：基于展示梯队按申万一级行业聚合家数（降序、同数按最高板），
   代表股为组内最高连板；单股缺失标"未分类"。
7. **涨停价核验**：涨停价 vs 理论涨停价（前收×1.1，创业/科创 ×1.2），偏差超半个最小变动单位
   （0.006 元）才计数提示（gaps）——低价股因 0.01 元 tick 舍入，涨幅比自然落在 9.6%-10.5% 属
   正常，不用比例阈值；仅计数提示，不自动剔除。
8. **读数辅助**（readings）：开盘即刻封板只数（首封时间 ≤09:25:59）、入梯股封单额末两位
   （代码/名称/封单额）由 Python 直接给出——报告读数要点中的此类可数断言**只引用 readings
   字段**，禁止自行数表。

## 二、报告纪律

1. 每项重要判断至少引用一项 Python 输出的数据证据（连板数、封单额、封单强度、主净、情绪指标）。
2. `null` 字段一律写"数据不足"，禁止补造；梯队表**按模板逐行输出**（表头行、分隔行、数据行各占
   一行），禁止把整张表压缩成一行。
3. 冰点（0 只入梯）时如实输出"无连板梯队"，仅呈现涨停池家数、炸板与情绪概览，不臆造梯队。
4. 用户要求按行业/题材筛选时：在**展示层**按 industry 精确匹配过滤输出子表，
   不得改取数、不得虚构记录；无命中则列出当日实际存在的行业让用户改选。
5. 区分事实（行情/封单/情绪）、计算（连板/新高/强度/聚合）与解释；禁止虚构未取得的数据。
6. **报告不展示查询行数/调用明细**（表名、返回行数、offset 分页参数等属内部诊断，用户不需要）；
   **禁止以任何变体形式复述行数清单**（"数据规模与覆盖""内部核验一致性"等段落/表格均不允许）；
   数据完整度只写定性结论与原因短语（如"名称缺失 1 只"），不写行数与片数；数据充分性只通过
   "数据口径与缺口"的自然语言表述传达。
7. **梯队读数中的排序/计数断言**（"末两位""N 只为开盘即封""前五"等）必须引用 `readings` 字段
   （Python 已算好）或逐行核对梯队表后再写，禁止凭印象写概数。
8. 完成全部维度后按 `report.md` 输出，不再推荐无关的追加工具调用。

## 三、口径缺口（如实声明，不补造）

- 涨停/连板为收盘口径（01 + LIMIT_VALUE_CLOSE 非空 + 价格比核验提示）；盘中开板次数不单独呈现，
  炸板率为全市场口径（mkt_limit_stats）。
- 名称/ST 过滤主源为申万行业成员快照（≈5221 成员，as_of 口径），md_security 2 片为补充
  （该表为全资产类型主档 >16.8 万行，无法整表分页）；两源均未命中的个股保留在池并在 gaps
  点名代码。
- 技术突破 = 20 日新高近似（30 自然日窗口，≈20 交易日，不含 D）；60/120 日新高不计算。
- LIMIT_TYPE='02'（跌停）；缺失或空结果按"数据不足"处理，不报 0。
- 行业为申万一级行业成员快照近似口径（无概念成分表）；北交所无申万成分映射。
- 公告滞后：请求日当晚运行常取到前一交易日榜面，以 as_of/gaps 为准。
