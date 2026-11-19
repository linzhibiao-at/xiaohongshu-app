---
name: xiaohongshu-growing-track
description: |
  小红书四大赛道判断：用真实搜索结果给母婴育儿/美食探店/旅行出行/家居生活打分，
  输出可解释的推荐结论。当用户问"选哪个赛道好做/哪个赛道适合我/赛道分析"时触发。
version: 1.0.1
metadata:
  openclaw:
    emoji: "\U0001F9ED"
---

# 赛道判断

## 目标

回答一个问题：**在四大赛道里，用户应该先做哪一个。**
结论必须由数据支撑，且能解释"为什么不是另外三个"。

## 输入

必须已获得主技能「开场五问」的回答。缺任何一项就先问，不要用默认值糊过去。

## 命令序列

```bash
# 命令都在 growing 根目录（有 SKILL.md 那层）执行
python3 scripts/growing_cli.py doctor

# 四个赛道逐个采集（分次执行，不要一次性跑完）
python3 scripts/growing_cli.py collect lane --lane 母婴育儿   --per-keyword 12 --detail-top 8 --with-authors
python3 scripts/growing_cli.py collect lane --lane 美食探店   --per-keyword 12 --detail-top 8 --with-authors
python3 scripts/growing_cli.py collect lane --lane 旅行出行   --per-keyword 12 --detail-top 8 --with-authors
python3 scripts/growing_cli.py collect lane --lane 家居生活   --per-keyword 12 --detail-top 8 --with-authors

# 打分对比（fit 由你根据用户回答计算，不要问用户要分数）
python3 scripts/growing_cli.py track compare --fit 母婴育儿=85,美食探店=45,旅行出行=40,家居生活=60

# 单赛道下钻，找具体切口
python3 scripts/growing_cli.py track lane --lane 母婴育儿 --fit 母婴育儿=85
python3 scripts/growing_cli.py track keywords --lane 母婴育儿
```

## 采集规模怎么定

| 用户时间预算 | 每赛道关键词数 | 每词条数 | 详情数 | 预计耗时 |
|---|---|---|---|---|
| 想快速看结论 | 1（主词） | 12 | 4 | 5-8 分钟 |
| 标准（推荐） | 4 | 12 | 8 | 20-25 分钟 |
| 精细对比 | 5-6 | 12 | 10 | 30-40 分钟 |

默认就是 4 个关键词（`lane.keywords[:4]`）。要改关键词数得显式传
`--keywords 词1 词2 ...`，否则缩成 1 个或加到 5-6 个都没效果。

`--with-authors` 会额外抓头部作者粉丝数，是判断"素人能不能突围"的关键，
**标准模式必须带上**，否则拿不到 `small_account_share`。

## 怎么读输出

`track compare` 的关键字段：

| 字段 | 含义 | 怎么用 |
|---|---|---|
| `score` | 0-100 综合分 | 只用于排序，不要向用户报"这个赛道 78 分所以好"，要讲分项 |
| `dimensions.demand` | 需求规模（中位互动映射） | <40 说明赛道整体流量低，起号慢 |
| `dimensions.saturation` | 供给饱和度，越高越卷 | >70 且 breakout <40 → 不建议新人首选 |
| `dimensions.breakout` | 素人突围空间 | <35 说明头部垄断，需要更强的差异化 |
| `dimensions.commercial` | 商业价值（静态经验权重） | 有变现目标时权重提高 |
| `dimensions.fit` | 你的匹配度打分 | 必须能说出这 10-20 分是怎么来的 |
| `evidence.median_liked` | 样本中位点赞 | 直接当作"正常水平"的锚点 |
| `evidence.viral_threshold` | 爆款门槛 | 复盘时判断一篇是否算爆款 |
| `evidence.title_homogeneity` | 标题同质化 0-1 | >0.35 说明套路高度重合，要换叙事视角 |
| `confidence` | high/medium/low | low 时结论必须加"待验证" |
| `gaps` | 数据里读出的供给缺口 | 直接转成给用户的"机会点"，每条都要能对应上数据 |

## fit 打分方法

从 50 分起算，按主技能的映射表加减，最后必须能在报告里列出加减项。
不确定时给 55-65 之间，不要给极端分。

## 输出模板

```markdown
## 结论先行
建议主做【母婴育儿】，副线做【家居生活】。置信度：中（母婴样本 96 条，家居 88 条）。
不推荐首选【旅行出行】：XXX（引用具体维度和数值）。

## 四赛道对比
| 赛道 | 综合分 | 需求 | 饱和度 | 突围空间 | 商业价值 | 匹配度 | 样本 | 置信度 |
|---|---|---|---|---|---|---|---|---|

## 为什么是母婴育儿
- 【数据】中位互动 xxx，高于美食 xxx 和旅行 xxx
- 【数据】前 8 篇里素人（粉丝<1万）占 xx%，说明新人能分到流量
- 【数据】标题同质化 0.xx，套路集中在"清单/避坑"，换"失败复盘"视角有空间
- 【经验】品牌投放密集，但接商单需要 5000 粉以上（待验证）

## 为什么不是另外三个
（每个赛道 1 条最关键的数据理由，不要笼统说"竞争大"）

## 具体切口（来自 track keywords）
| 关键词 | 样本 | 中位赞 | P90 | 标题同质化 | 作者数 |
|---|---|---|---|---|---|

## 下一步
1. 确认主赛道后做定位三件套（Phase 2）
2. 需要补采的关键词：...
```

## 常见错误

- **只看 score 不看分项**：score 高可能只是 commercial 高，不代表好起号。
- **忽略 confidence**：样本 12 条的结论不要当结论讲。
- **把"竞争大"当理由**：必须说清是饱和度、突围空间还是同质化的问题。
- **账号标签污染**：用户账号长期刷某类内容，搜索结果会偏向该赛道。
  报告里要提示"结果受当前账号兴趣标签影响，建议 2-4 周后复采"。
