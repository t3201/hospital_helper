# slots/profiles — 档位默认态势结论(给决策者)

> `build_profiles.py` 重跑即可复现; 指纹 `content-fingerprint:a08740dc5682ebf9` 由 `level_defaults.json` / `slot_profile.json` / `variance_report.json` 共享。
> 数据: 10 家采样医院 / 702 settled 单元 / **每单元仅 1 次观测(单快照, TTL 90min)** → 全部档位值均为**低样本先验**。

## 一句话结论
**用"级别默认态势"推定某家医院"有没有号", 解释力只有约 12.3%, 对"一级甲等"档基本无效(档内率极差 0.778) → 默认值只能用作 `tension` 先验排序, 绝不可当作"有号/无号"的结论展示给用户; 一甲档运行期必须实抓。**

## 证据
1. **方差分解**(n=702, has_slot 二值, 总方差 p(1-p)=0.241): between_hospital **12.3%** / within_hospital_by_dept_category **22.6%** / residual **65.2%** — 医院身份只解释约 1/8, 大部分方差是科室/时点级残差。
2. **组内离散反例**: 同属"一级甲等|综合医院"的三家, has_slot_rate = **0.778 / 0.0 / 0.333(极差 0.778)**; "三级甲等|综合医院"两家极差仅 **0.148**。"同档" ≠ "同态势"。

## 档位样本与置信度
| 档位 | n_hospitals | n_units | has_slot_rate | no_slot_rate | confidence |
|---|---|---|---|---|---|
| 一级甲等|综合医院 | 3 | 65 | 0.231 | 0.769 | high |
| 三级乙等|综合医院 | 1 | 98 | 0.612 | 0.388 | mid |
| 三级甲等|专科疾病防治院 | 2 | 56 | 0.661 | 0.339 | mid |
| 三级甲等|综合医院 | 2 | 383 | 0.65 | 0.35 | high |
| 二级乙等|综合医院 | 1 | 43 | 0.721 | 0.279 | low |
| 二级甲等|综合医院 | 1 | 57 | 0.474 | 0.526 | mid |

- 置信度规则: low=1 家且 <50 单元; mid=2 家或 50-300 单元; high=≥3 家或 ≥300 单元。
- **每档 n_hospitals 仅 1–3、每单元仅 1 次观测 → 全部为低样本先验**; confidence 只反映采样量, 不代表统计可靠性。

## 字段语义(B2 修正)
- `bar_variant_rate` = {days14, short2, long_gt14, nobar, unknown}, 分母=settled 单元(每档报告 n_settled/n_ok/n_no_slot), 脚本断言 sum≈1。
- `no_slot_rate` = 单次观测 no_slot 比例(原 `perennial_no_slot_rate` 命名有误, 已更名); `perennial_no_slot` 按 RANKING_SPEC §5(≥3 次观测全失败)独立保留, 当前一律 unknown/false(仅 1 次观测, 无法判定)。
- `by_dept_category` 只含 `tab_kind=category` 的真科室大类; 社区 site/campus tab 单出 `by_site`(分母口径见 `dept_category_meta`)。
- `unobservable`: 显式注明 earliest/price 读不到的原因(L2=`l2_nobar` 变体无日期条 / 价格只在明细层), 非计算 bug; 补救: 进明细层(医生主页)抓取, 成本 **5.8–6.5s/次**。
