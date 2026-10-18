# SLOTS — 号源层（5 层解析 + probe 层, 缓存优先, 全标注来源）

`app/slots.py::SlotResolver.resolve(hid, dept_id, canonical, level, type, l1_cat)` → SlotResult。
**设计原则（RANKING_SPEC §4.5/§5）**：宁可标注"未核实", 绝不把先验/合成数据当实时事实; 每条结果带 `slot_source` + `provenance`（出处文件/时间）。

## 1. 五层解析顺序

| 层 | 来源 | 新鲜度判定 | 标注 |
|---|---|---|---|
| 1 | `slots/profiles/slot_profile.json`（§5 schema; 7,614 条 (dept_id,hospital_id) 画像: 670 observed + 6,944 level_default） | `last_fetched_at` / `captured_at` ≤ **TTL 90min** 才可用 | `cache` + `detail=observed`（已核实）或过期 → `stale_reference`（不当实时呈现） |
| 2 | `slots/out/<hid>-<slug>.json`（fetch_slots 观察快照; 本包含 10 家抽样医院） | `captured_at` ≤ TTL 90min | 同上 |
| 3 | 【`--live --device`】子进程调 `slots/fetch_slots.py`（**v2**, `--condition` 统一接口）批量实时抓取 → 读 `slots/out/condition_runs/` 的 run 摘要 + 六字段记录 | 抓取时间戳（`captured_at`, 恒新鲜） | `live`（verified） |
| 4 | `slots/profiles/level_defaults.json`（**档位先验**: 按 等级\|类型 分层的 no-slot 率/最早可约/fee_tier, 带 stratum/n_units/confidence） | 无时间概念（非实时） | `cache` + `detail=level_default`, note 恒含"非实时, 不当作号源事实" |
| 5 | `app/fixtures/stub_slots.json`（内置合成小样本） | — | `stub`, 恒标注"号源未核实" |

TTL 过期处理：不丢弃, 降级为 `stale_reference`（结果里带过期明细: 年龄分钟数/出处/过期时长）, 展示层标注"已过期, 未核实"。

## 2. SlotResult 字段（result.json `slots.<hid>`）

```jsonc
{
  "hospital_id": "SH-003", "dept_id": "D02633", "dept_name": "内科",
  "slot_source": "cache",              // live | cache | stub
  "detail": "level_default",           // observed | level_default | ...
  "status": "ok|no_slot|unknown",
  "verified": false,                   // 仅 live / 新鲜 observed 为 true
  "earliest_date": "2026-10-07",       // 可空
  "earliest_days": 5.0,
  "fee_tier": "50|100+|unknown",            // 众数档(live=记录众数; 档位先验=stratum 众数, 样本少时可能退化)
  "fee_tiers": { "15": 0.02, "25": 0.02, "50": 0.003 },  // 费档分布(2026-10-05 新增; live=六字段记录占比, 档位先验=stratum 观察占比)
  "fee_min_tier": "15",                     // 最低档(有观察的最低费档; 预算超限判定用最低档而非众数)
  "tension": null,                     // 紧张度（有号但稀缺程度, 0~1）
  "perennial_no_slot": false, "perennial_no_slot_rate": null,
  "has_slot_rate": 0.65,
  "confidence": "high|mid|l",
  "provenance": { "file": "slots/profiles/level_defaults.json", "stratum": "三级甲等|综合医院", ... },
  "note": "档位估算（stratum=..., n_units=383, confidence=high; 非实时, 不当作号源事实）",
  "stale_reference": null              // 过期时含 {age_min, captured_at, source, ...}
}
```

## 3. 查询预算与早停（main ⑤ 段）

- 预算：**前 12 家**（按静态分序; `BUDGET_FETCH_DEFAULT=12`）, 每家取其目标科室集合的 best SlotResult。
- **代表科室选择 `_best_info`（2026-10-06 修订, 事实优先）**：已核实有号 > 已核实无号 > 未核实。旧规则下已核实 no_slot（earliest_days=None→99）与未核实 level_default（earliest_days=p50 估计值）同分、未核实者凭 earliest_days 胜出, 掩盖已核实无号事实（9dc8: 华山 耳鼻喉科实查 no_slot, 却上报未核实的兄弟科室「声带疾病专病门诊」(live error)）→ 已核实事实（即使为负）必须优先于未核实估计。
- 2026-10-06 起取消早停：live 抓取已在 prepare_live 批量完成(单次 adb 会话), 逐院 resolve 为纯缓存查找零设备开销; 早停会丢弃后段医院的已核实 no_slot(u=0.25) 事实。仅保留预算 12 家。
- 查询顺序即 `slot_queries`（§4.3 SlotQuery 序列, 落盘可审计, 仅生成不发起; 发起只在第 3 层 live）。

## 4. 未核实批次 → §4.5 回落（recommend 层）

- `fallback_all_unverified = true`（本批 0 条已核实）→ top_list 前 20 项**与 stage-1 静态排序同序**（断言 B 强制）, 全部逐行标注"号源未核实（来源明细）", 报告 §6 顶部横幅明示"不假装实时"。
- 号源效用 `slot_utility`：verified ok → `max(0.6, 1-0.03×earliest_days) × (1-0.4×tension)`; no_slot → 0.25; **unverified → 中性 1.0（仅标注, 不决定生死）**; **未核实折扣(2026-10-06)**: 本轮含已核实号源时未核实候选 `u×0.99`(1% 让位; 全未核实轮仍全部 1.0, §4.5 回退静态序不变)。
- 系数（0.03/天、0.4×tension、0.25）为启发式未调参（已知局限, RANKING_SPEC §3/§7）。
- **性价比因子（2026-10-05 新增）**: LLM `price` 权重分量 >0 且设 price_cap 时: 已核实 ok → `u ×= (1+price·(2·p_in−1))`（p_in=可核实费档中预算内占比, 钳制 [0.85,1.15]）; 未核实 → 仅标注"该档位/观察样本中预算内费档占 x%（未核实, 不参与排序）"。费用档超预算判定改用 `fee_min_tier`（最低可核实档）而非众数档, 避免"有便宜档但仍被误判超预算"。

## 5. live 抓取现状（2026-10-05 接入 v2; 2026-10-06 两阶段 probe/deep）

### 5.1 两阶段 live（2026-10-06）
- **Phase A probe**（`prepare_probe`）：v2 `--mode probe`，L2 科室页单页信号（「去预约」按钮/日期条/空态），
  不逐医生点主页。实测 60~135s/院（full 85~494s; full 的开销 = 逐医生主页 ~10s/人 × 医生数 + 列表枚举滚动）。
  日期条屏外项在 XML 中（bounds=[0,0][0,0], 文档序）→ 一次 dump 读全，**无需横滑**；
  未见「去预约」按钮才有限上下滑（≤4 次, 找到即停）。
- **信号→状态**：ok(按钮)→verified ok / bar_hint(日期条有号无按钮)→verified ok(bar_only 弱信号) /
  no_slot(空态或条全无号)→verified no_slot / error→未核实（不进 _probe_results）。
- **Phase B deep**（`prepare_live`）：初排前 `--deep-n`(5) 名走 full 六字段（医生/费档/时段），终裁素材；
  deep 结果覆盖 probe（resolve 层序: profile → **deep live** → 快照 → **probe** → 档位 → stub;
  2026-10-07 f89d 修复: deep 前置到快照之前 + 写回后作废内存缓存 + 无费档结论从快照回填 fee_from_stale + 写回覆盖时继承旧费档）。
- **回写**：probe 与 deep 的 verified 结果都回写 slots/out/<hid>-<slug>.json（source=live_v2_probe / live_v2_condition），
  下次运行 TTL 90min 内命中 cache/observed（迭代收敛）；同 dept_id 旧条目清除（深结果覆盖 probe）。
- **run 文件**：condition_runs/run_<ts>.json 带 `mode` 字段（probe/condition）；`_parse_live_runs` 只认
  mode∈(None,condition)，`_parse_probe_runs` 只认 mode=probe —— 两阶段 run 互不污染。
- **CLI**：`--probe-n`（默认 12）、`--deep-n`（默认 5）、`--live-timeout` 总预算按 55%/45% 切分。
- **加速与稳定（2026-10-07）**：
  1. probe: 日期条每日 chip 均明确有号/无号, **全无号 => 直接 no_slot, 跳过子 tab 扫描**(每组省 ~30-60s, 深核仍复核 top 候选);
  2. 子 tab tap: **连续 3 次卡集不变提前判 unchanged**(死 tab ~53s->~20s, retry 仍给 6 轮保慢切换);
  3. L2 卡枚举: **稳定判定仅看卡名序列**(旧版含末 3 卡 y 几何, 底部抖动使 stable 永不达 2 => 白滚满 12 次;现到底 ~8s);
  4. dump: **exec-out 快速通道**(省 pull 往返) + 失败回退 pull; 重试等待 7s->3s(单次 flaky dump 不再卡 7s);
  5. 导航: LOADING 轮询 7s->2s; 点院后固定 5s 等待 -> 轮询 L1 就绪(2s 间隔, 至多 10s)；
     各处 swipe settle 1.6->1.0/0.9, 回顶步长 900->1300px。
  实测(2026-10-07 真机): SH-084 耳鼻喉 no_slot 组 148.8s->90.1s; 同页复用时单组 probe 32.9s; L2 枚举到底 8.3s(2 次 swipe)。



- **引擎**: `FETCH_V2 = slots/fetch_slots.py`（v2, S20 `--condition` 统一接口）；`FETCH_SCRIPT = slots/fetch_slots_v1_20261004.py`（v1）保留为**离线全院批量采集工具**（姿态层/批次脚本用），app live 不再调用。
- **触发方式**: `--live --device <serial>`（无 `--device` 时 v2 用其默认 476cf991）。主流程在号源循环**前**把前 12 家候选院的目标科室一次性打包为条件组, `SlotResolver.prepare_live()` 用**一次 adb 会话**跑完（nav_state 复用, 最小回溯）; 抓取纯 ADB + UI XML 文本匹配, **零模型参与**（v2 内置 zero-model 守卫 + CJK argv 守卫）。
- **条件路由**: dept_id（归一化）→（tab 名, 页面 raw 科室名）由院 manifest 直接给出（`features.bookable_departments.value.l1`）；同一 dept_id 多次出现时优先可预约 kind（clinic/specialty_clinic/screening）→ 无括号 plain 名。条件经 `--condition-file`（UTF-8 JSON, 免损坏通道）下发, CJK 不经 argv。
- **采集深度** `--live-slot-type`: 缺省 = 全部子 tab（专家/普通/专病, 最全最慢, 实测约 **5~15 分钟/科室**）; `expert` = 仅专家门诊（快速档, 约 1/3 耗时, 如 SH-084 眼科 80 条/452s）。
- **预算预警（2026-10-05 新增）**: `prepare_live` 前估算 = 条件组数 × 每组耗时（全子tab 540s / expert 240s / normal 420s / specialty 480s）；超过超时上限时打印醒目预警（**到点后后续组不跑; v2 逐组增量落盘, 已完成组保留**）, 建议 expert 档 / `--live-timeout` 调大 / 离线批量积累。
- **超时取证（2026-10-05 新增）**: `TimeoutExpired` 时读 `slots/out/nav_state.json` 打印"超时时 v2 已进行到 哪院/哪 tab/哪科室/哪子tab", 便于定位卡点。
- **输出解析**: 读 `slots/out/condition_runs/run_<ts>.json`（每条件组 status/per_sub_tab 医生明细）+ `<ts>_all.jsonl`（六字段记录）→ 聚合为 SlotInfo：有记录 → `status=ok` + 最早日期/费档/可约医生数；页面无号 → `no_slot`（诚实, 非未核实）；组失败 → `unknown`（降级档位/stub）。
- **前置条件**: ADB 设备 + 随申办 App 在线；子进程超时上限可配置 `--live-timeout <秒>`（默认 3600=60 分钟, 硬杀余量 +900s 兜底），任何失败均降级不崩流水线（§4.5 诚实标注）。
- 本包含 v2 及其伴生模块 `six_fields.py`（六字段 schema, 纯标准库）；`slots/out/condition_runs/` 为运行期 live 输出目录（首条真实样例: `run_20261005T112842.json`, SH-084 眼科专家门诊 80 条）。
- **执行顺序（2026-10-05 修复）**: 条件组按**静态分序**下发且 v2 **保持输入序执行**（第1名医院先跑, 排名靠前者优先拿到真实号源）。原 v2 `expand_conditions` 曾按 (hid,tab,dept) 字母序重排以"最小化换医院", 导致静态第1名实际排到第6位才跑 —— 已改为保持调用方顺序（导航代价 << 优先级代价）。
- **live 批量每院只抓主科室（2026-10-06 修订）**：`prepare_live` 只把每院第 1 个目标科室（tier 最高 = LLM 首选 = 推荐行的挂号入口）打成条件组; 旧多科室展开（每院 ≤5）把批次吹爆（9dc8: 12 院 × ~5 科 = 54 条件 → 合并 22 组, 60 分钟只覆盖 6/12 院）且多数兄弟科室根本抓不到。兄弟科室 resolve 走缓存/档位（零设备开销）, 真实数据由离线 fetch_slots.py 批量积累（迭代收敛不变）。
- **live 覆盖 `final.live_coverage`（2026-10-06 新增）**：prepare_live 记录 `{n_routes(本批医院数), n_parsed(解析到 live 结果的(院,科)数), n_units(实跑条件组数), stop_reason, run_file}`; web 端 n_parsed<n_routes 时显示警示横幅"实时核实仅覆盖 X/Y 家医院（deadline 到点, 后续组未跑）; 未覆盖医院按未核实处理"。
- **增量落盘（2026-10-05 新增）**: v2 每完成一个条件组即 flush 合并记录快照 `<ts>_partial.jsonl` + run 摘要 `run_<ts>.json`（tmp+rename 原子写）；超时/硬杀只损失剩余未跑组, 已完成组全部可用。正常跑完时终版仍写 `<ts>_all.jsonl` 并删除 partial。
- **超时优雅停止（2026-10-05 新增）**: `prepare_live` 把超时上限换算成 v2 `--deadline`（ISO 时间戳）下发；v2 每组开始前检查, 到点 `stop_reason='deadline'` 干净退出（正在跑的那组跑完为止）, 不启动后续组。真机验证（run_20261005T223424）: 眼科门诊 60 条完成后 deadline 到点, 下一组未启动。
- **live→静态回写（2026-10-05 新增, 迭代收敛）**: live 解析后 `_persist_live_snapshots()` 把 verified(ok/no_slot) 的 (院,科) 回写观察快照 `slots/out/<hid>-<slug>.json`（L2 层, 本包唯一允许写 slots/ 的点; 同 dept_id 的旧别名条目自动清除）→ 下次运行 TTL(90min) 内直接命中 cache/observed 已核实, 不重复实抓；常用科室的静态号源特征随使用逐渐逼近动态真实。未核实结果不回写（不污染缓存）。

## 6. 号源数据出处（Track A 抽样, 只读）

- 10 家分层抽样医院 / 702 个 (院,科,门诊类) 单元实测 → 670 observed 画像 + 按档位外推 6,944 level_default。
- 方差分解（抽样）：医院间 12.3% / 科室级 49.7% / 残差 38% —— 科室级差异远大于医院级, 故档位先验只作排序张力, 不作事实。
- 一级甲等|综合医院 档位先验不可靠（抽样命中率 0.778/0.0/0.333 离散）→ 一级甲等运行时**应走 live 抓取**, 档位仅作兜底标注。
- 价格 tier 仅在医生明细层可观测（姿态层无）; 多数 fee_tier 为 unknown 属预期, 报告如实标注。
