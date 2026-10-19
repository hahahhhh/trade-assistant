# WH6 交易辅助工具 Playbook

版本：`2026-10 initial`

本 Playbook 用于在 Windows + WH6（进程 `mytrader_spqh.exe`）环境中运行当前第一版交易辅助引擎。

## 1. 工具边界

工具只做：

- 读取 WH6 窗口截图；
- OCR 识别合约、买一、卖一、最新价等行情字段；
- 计算价格模式、最小跳数和人民币风险；
- 检查交易前计划；
- 保存识别快照和交易前计划；
- 交易后做统计分析。

工具不会：

- 点击买多、卖空、平仓；
- 向 WH6 写入价格或手数；
- 自动提交订单；
- 自动移动止损；
- 自动修改交易规则；
- 使用 A/B 模型决定是否交易。

A/B 模型当前只留档，未实现。

## 2. 目录结构

```text
workspace-trade-summary-analysis/
├── trade_assistant.py                    # 风险与交易计划检查引擎
├── wh6_screen.py                         # WH6 窗口/截图 OCR 适配器
├── analyze_trades.py                     # 交易账本分析脚本
├── trade_form.py                         # tkinter 按钮表单
├── trade_assistant_config.json           # 品种风险和枚举配置
├── TRADE_ASSISTANT_README.md             # 基础使用说明
├── WH6_TRADE_ASSISTANT_PLAYBOOK.md       # 本文档
├── AB_MODEL_ARCHIVE.md                    # A/B 留档计划
└── tests/                                 # 单元测试
```

运行时生成的快照、报告和 OCR 留档不应放入源码压缩包。

## 3. Windows 环境准备

打开 PowerShell，确认 Python：

```powershell
python --version
```

安装 Python 依赖：

```powershell
python -m pip install pywin32 psutil Pillow pytesseract
```

安装 Tesseract OCR。安装后确认：

```powershell
tesseract --version
```

需要安装中文简体和英文语言包：

```text
chi_sim
eng
```

如果 `tesseract` 不在 PATH，后续命令使用：

```powershell
--tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

## 4. 第一次运行前检查

1. 启动 WH6。
2. 确认目标窗口由进程 `mytrader_spqh.exe` 创建。
3. 将 WH6 行情窗口保持可见，避免最小化。
4. 确认右侧行情栏包含合约、卖出/买入、最新价。
5. 先不要使用 `--watch`，先做单次识别。

单次捕获并 OCR。默认推荐 `app`，会合并捕获 `mytrader_spqh.exe` 所属的所有可见窗口，即使你切换到其他应用也能读取 WH6；`window` 只捕获单个窗口，`monitor` 捕获所在显示器：

```powershell
python workspace-trade-summary-analysis\wh6_screen.py `
  --capture `
  --capture-mode app `
  --save-capture C:\wh6_data\last_capture.png `
  --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

`app` 模式会优先使用 Windows `PrintWindow` 读取 WH6 窗口内容，因此你切换到其他应用后，仍可读取 WH6；如果 WH6 的 GPU 渲染不支持该方式，程序会自动回退到屏幕截图。它不会捕获其他应用的内容。可先列出 WH6 窗口：

```powershell
python workspace-trade-summary-analysis\wh6_screen.py --list-windows
```

如果只想识别标题中含有特定文字的窗口：

```powershell
python workspace-trade-summary-analysis\wh6_screen.py `
  --capture `
  --capture-mode app `
  --window-title "文华交易客户端" `
  --save-capture C:\wh6_data\last_capture.png `
  --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

`app` 模式不会要求截图等于 1920×1080；它保存的是 WH6 窗口本身的图像。

预期输出是 JSON，至少应看到：

```json
{
  "variety": "V",
  "contract": "PVC2701",
  "bid1": 4902,
  "ask1": 4903,
  "last": 4903,
  "validation": {
    "valid": true
  }
}
```

注意：WH6 画面中的 `PVC2701M` 会规范化为 `PVC2701`，品种会规范化为 `V`。

## 5. 从截图文件识别

如果窗口捕获失败，可以先用系统截图工具保存 PNG，再运行：

```powershell
python workspace-trade-summary-analysis\wh6_screen.py `
  --image C:\path\wh6.png `
  --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

如果只想识别截图中的局部区域：

```powershell
python workspace-trade-summary-analysis\wh6_screen.py `
  --image C:\path\wh6.png `
  --region 1800,0,390,500
```

`--region` 格式是：

```text
x,y,width,height
```

坐标相对于输入截图左上角。

## 6. 持续识别和行情留档

每 2 秒捕获一次：

```powershell
python workspace-trade-summary-analysis\wh6_screen.py `
  --capture `
  --watch `
  --interval 2 `
  --out C:\wh6_data\market_snapshots.jsonl `
  --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

停止方式：

```text
Ctrl+C
```

自动刷新只更新行情识别记录，不会覆盖已经确认的交易计划，也不会操作 WH6。

## 7. 交易前检查的最小流程

### 推荐：一条命令完成 识别 → 定价 → 风险检查

```powershell
python workspace-trade-summary-analysis\wh6_screen.py `
  --capture `
  --capture-mode app `
  --save-capture C:\wh6_data\last_capture.png `
  --ocr-effort auto `
  --plan-out C:\wh6_data\plan.json `
  --plan-check `
  --direction long `
  --size 1 `
  --price-mode opponent `
  --stop-loss 4863 `
  --take-profit 4950 `
  --smc-tag FVG `
  --htf-bias long `
  --snapshots C:\wh6_data\pre_trade_snapshots.jsonl `
  --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

这条命令会：

1. 捕获 WH6 应用窗口并 OCR 右侧行情栏；
2. 把识别到的买一/卖一/最新价写入 `plan.json`；
3. 用 `--direction` / `--price-mode` 解析入场价；
4. 计算跳数与人民币风险，与品种单笔上限比较；
5. 输出 `PASS` / `WARN` / `REJECT`；
6. 把计划、检查结果和 OCR 来源写入快照 JSONL。

注意 `contract` 会被规范化：WH6 显示的 `PVC2701` 在计划中写成 `V2701`（风险配置按品种 `V` 匹配），原始标签保留在 `contract_label` 字段，便于回溯。

### OCR 速度档位

| `--ocr-effort` | 最坏 Tesseract 调用次数 | 适用 |
|---|---:|---|
| `fast` | 1 | 光线稳定、只想快速取盘口 |
| `auto`（默认） | 7，通常 1 | 日常使用；命中即停 |
| `thorough` | 11 | 识别失败时排查 |

`auto` 会先只识别右侧行情栏的灰度图；四个字段齐全且置信度达标时立即停止，不再跑其他组合。

### 手工方式：先写计划 JSON

如果不用自动生成，也可以手写计划并单独检查：

```json
{
  "variety": "V",
  "contract": "V2701",
  "direction": "long",
  "size": 1,
  "price_mode": "opponent",
  "stop_loss": 4863,
  "take_profit": 4950,
  "smc_tag": "FVG",
  "htf_bias": "long",
  "is_add_on": false,
  "market": {
    "bid1": 4902,
    "ask1": 4903,
    "last": 4903,
    "ocr_confidence": 0.98
  }
}
```

运行检查：

```powershell
python workspace-trade-summary-analysis\trade_assistant.py `
  check C:\wh6_data\plan.json
```

如果检查通过，程序输出 `PASS`；存在软警告输出 `WARN`；违反硬规则输出 `REJECT`。

其中一个软警告是“风险达到单笔上限的 95% 以上”。例如 V 用 40 点止损正好 ¥200，等于上限且尚未计入手续费/滑点，实际净风险会超过 ¥200，因此会输出 `WARN` 而不是 `PASS`。想留出成本余量，就把止损收到 30 点（¥150）左右。

每次检查会追加保存交易前快照。默认位置：

```text
workspace-trade-summary-analysis/data/trade_assistant/pre_trade_snapshots.jsonl
```

## 8. 价格模式

计划字段 `price_mode` 支持：

| 值 | 含义 |
|---|---|
| `manual` | 手工输入成交价 |
| `opponent` | 多单取卖一，空单取买一 |
| `queue` | 多单取买一，空单取卖一 |
| `last` | 使用最新价 |
| `offset` | 基准价加减 N 个最小跳 |

`offset` 示例：

```json
{
  "price_mode": "offset",
  "base_price": 4902,
  "offset_ticks": 2
}
```

所有价格必须落在品种的最小跳动网格上。

## 9. 风险口径

风险按品种配置，不只按点数比较：

```text
规范化止损距离
= ceil(abs(入场价 - 止损价) / 最小跳动) × 最小跳动

计划风险
= 最小跳数 × 每跳人民币价值 × 手数
  + 手续费/滑点缓冲
```

当前配置：

| 品种 | 每点价值 | 最小跳动 | 每跳价值 | 单笔上限 |
|---|---:|---:|---:|---:|
| V | ¥5 | 1点 | ¥5 | ¥200 |
| LH | ¥16 | 5点 | ¥80 | ¥200 |
| FG | ¥20 | 1点 | ¥20 | ¥200 |

例子：

- V：40 点约为 ¥200；
- LH：2 跳为 ¥160，3 跳为 ¥240，3 跳会被拒绝；
- FG：10 点为 ¥200。

### 两种风险预算方式（每品种二选一）

单笔上限不再只有固定金额一种：

| 模式 | 取值 | 适用 |
|---|---|---|
| 固定金额 | `single_trade_limit_rmb` | 资金规模小、想硬性封顶 |
| 总资金百分比 | `risk_percent` × 总资金 | 资金变化时上限自动跟随 |

规则：

- **二选一，不叠加**。百分比模式下不会回退到固定金额——宁可拒绝，也不静默用错上限；
- 总资金存在 `data/account.json`，由表单里的「总资金（元）」填写并记住，**不写入会进 Git 的配置文件**；
- 百分比模式缺少总资金时，检查直接 `REJECT`；
- 换算结果与依据会写进快照（`budget` 字段），事后能解释当年的上限为什么是 1000。

日亏损上限同理，由 `global_limits.daily_loss_mode` 控制（`fixed` 用 `daily_loss_limit_rmb`，`percent_of_equity` 用 `daily_loss_percent`）。注意：当前版本只**计算并显示**日亏损上限，日内累计规则尚未实现。

JD、RU、MA、C、JM、RB 当前配置为未启用，检查会拒绝通过，直到补齐风险参数。

识别支持的品种写法（中文名或代码都可以）：

| 中文名 | 代码 | 状态 |
|---|---|---|
| 聚氯乙烯 / PVC | V | 已配置 |
| 生猪 | LH | 已配置 |
| 玻璃 | FG | 已配置 |
| 螺纹钢 / rb | RB | 待填风险参数 |
| 鸡蛋 / 橡胶 / 甲醇 / 焦煤 / 玉米 / 铁矿石 | JD / RU / MA / JM / C / I | 待填风险参数 |

五档盘口（卖五…卖一、买一…买五）会取「卖一」作为卖一价、「买一」作为买一价。识别到未配置的品种时不会猜成别的品种，而是明确提示“风险参数未启用”并拒绝。

当前手续费和滑点缓冲配置为 0，代表未录入真实期货公司成本；风险结果是未计成本的下界。

## 10. 识别失败处理

### 找不到 WH6 窗口

检查：

- WH6 是否已经启动；
- 进程名是否确实为 `mytrader_spqh.exe`；
- 窗口是否可见、未最小化；
- 是否有权限差异导致 Python 无法读取窗口；
- 是否启动了多个 WH6 实例。

可以改用截图文件模式继续工作。

### OCR 识别为空或字段错误

检查：

- Tesseract 是否安装 `chi_sim` 和 `eng`；
- 截图分辨率是否足够；
- WH6 是否被缩放或遮挡；
- 只截取右侧行情栏以减少噪声；
- 运行结果中的 `ocr_text`；
- `ocr_confidence` 是否低于配置阈值。

不要直接使用低置信度结果下单。当前工具会把低置信度标为警告，需人工核对。

### 买一大于卖一

这通常是 OCR 把数字读错。重新截图或缩小识别区域，不要手动把结果当作有效行情。

### 品种未配置

识别可以成功，但交易计划会被拒绝。需要在：

```text
workspace-trade-summary-analysis/data/trade_assistant_config.json
```

补充该品种的：

- 每价格单位价值；
- 最小跳动；
- 每跳价值；
- 单笔风险上限；
- 手续费/滑点缓冲。

修改后必须运行测试，并确认：

```text
每点价值 × 最小跳动 = 每跳价值
```

## 11. 交易后分析

使用当前本地账本生成报告：

```powershell
python workspace-trade-summary-analysis\analyze_trades.py `
  workspace-trade-summary-analysis\data\trade_ledger.csv
```

输出：

```text
workspace-trade-summary-analysis/data/trade_assistant/report.md
workspace-trade-summary-analysis/data/trade_assistant/report.json
```

报告目前用于：

- 品种/方向/SMC 标签分组；
- 胜率和点数统计；
- 风险字段覆盖情况；
- 候选纪律违规标记；
- 后续优化假设。

小样本分组不能直接升级为交易规则。必须结合未来影子样本验证。

## 12. 按钮界面（tkinter）

不想每次敲命令行参数时，用图形表单：

```powershell
python workspace-trade-summary-analysis\trade_form.py
```

界面分成四块，顺序就是你下单时的思考顺序：

```text
┌ 行情条：合约 · 卖一 · 买一 · 最新 · 置信度        [刷新][手工填价][配置][历史]
├──────────── 下单票（左） ───────────┬──── 风险面板（右） ────┐
│ ① 方向   买多 / 卖空   加仓        │  PASS / WARN / REJECT  │
│ ② 入场   对手价/排队价/最新/手工   │  每手风险 / 计划风险   │
│ ③ 止损   跳数 + 价格（决定风险）   │  单笔上限 / 剩余预算   │
│ ④ 手数   1R 推导 + 套用建议         │  [██████░░░] 占比      │
│ ⑤ 止盈   = 止损 × R 倍数           │  检查发现（阻断/警告） │
│ ⑥ 标签   SMC + HTF                 │  [检查计划][保存计划]  │
└─────────────────────────────────────┴────────────────────────┘
  状态栏                                     ↓
  [行情详情] [OCR 原文] [本次检查]
```

操作顺序：

1. 点「刷新行情」（或按 `F5`）读取 WH6 行情栏；
2. 选方向、价格模式，入场价自动填入；
3. 调止损跳数，右侧「每手风险 / 计划风险 / 占比」实时变化；想用非盘口价入场，直接改「入场价」框——会自动切到「手工价」，止损止盈按新价重算，检查时不会覆盖回去；
4. 点「套用 N 手」按 1R 定手数；
5. 点「检查计划」（`Ctrl+Enter`）看右栏大字结果；
6. 结果非 `REJECT` 时点「保存计划」（`Ctrl+S`）写入 `data/plans/` 并追加快照。

关键交互规则：

- **结论只来自检查**：`PASS/WARN/REJECT` 只有点了检查才出现；之后任何字段一改，徽章立刻变「已过期，需重新检查」，保存按钮同时禁用。
- **风险实时**：右栏数字与进度条随输入更新，不需要先检查；超过上限显示红色「超出上限 ¥X」。
- **快捷键**：`F5` 刷新行情、`Ctrl+Enter` 检查、`Ctrl+S` 保存、`Alt+1` 买多、`Alt+2` 卖空。
- **低置信度**：行情条变浅黄，提示需人工核对。

### 快速切换品种（缓存行情）

头部最左侧有「品种」下拉，列出所有已启用品种。选中即切换，**不会重新 OCR**：

- 每个品种首次识别后，行情会缓存到 `data/trade_form_state.json`；
- 切回已识别过的品种时直接用缓存，瞬间完成，顶部显示「缓存 3 分钟前」；
- 切换后止损、止盈、1R、风险面板都会按新品种重算，旧检查结论标记为过期；
- 缓存行情会在检查时带一条提示「使用缓存行情，请核对价格是否仍然有效」；
- 没缓存过的品种会提示「请在 WH6 切到该合约后按 F5 识别一次」。

这样盘中在两个品种之间来回看时，只有第一次需要等识别。

### 历史计划页面

点「历史计划」打开独立窗口，按品种分组浏览已保存的计划：

```text
V（3）        ← 可折叠的品种分组
  ├ 2026-10-18 20:15  PVC2701  多  1  4903  4873  4963  ¥150  PASS  FVG  多头
  └ 2026-10-18 19:02  PVC2701  空  1  4902  4932  4842  ¥150  WARN  Sweep 空头
RB（1）
  └ 2026-10-17 22:41  RB2701   多  1  3086  3076  3106  ¥100  PASS  OB   多头
```

- **筛选**：左上角下拉选单个品种或「全部」；表格下方显示总条数与各品种计数；
- **分组**：每个品种一行汇总，点箭头折叠；
- **删除选中**：先删 `data/plans/` 里的计划文件；对于只存在于快照、没有计划文件的记录，会单独询问是否从快照中删除（删除前自动备份 `.bak`）。汇总行会显示「其中 N 条仅在快照中」。若删除后仍有内容完全相同的记录，会提示可能是重复保存；
- **导出 CSV**：选中则导出选中项，未选中则导出全部；UTF-8 BOM 编码，Excel 打开中文不乱码；
- **打开目录**：直接打开 `data/plans`。

数据来源是计划文件与快照的自动匹配：保存计划时会把计划 JSON 写入文件并追加一条快照，历史页面用计划内容的指纹把两者对上，因此能同时显示计划参数和当时的 `PASS/WARN/REJECT` 与风险金额。只跑过 `--plan-check`、没有写计划文件的记录也会显示，状态正常但计划文件一栏为空。

### 品种配置按钮

点「品种配置」可以直接改风险参数，不用手编辑 JSON：

- 下拉选择品种（V / LH / FG / RB / JD / RU / MA / C / JM）；
- 风险预算方式二选一：固定金额，或按总资金百分比（填 2 表示 2%）；选百分比时固定金额输入框会自动禁用；
- 填每点价值、最小跳动、单笔上限，**每跳价值会自动算**，不会出现三者对不上的情况；
- 手续费/手、滑点缓冲/手可留空，按 0 处理；
- 勾「启用该品种」后才会参与风险检查；
- 保存时先写一份 `.bak` 备份，再写入 `data/trade_assistant_config.json`。

保存后表单立刻按新参数计算，不需要重启。如果填了 0、负数或非数字，会弹窗指出具体是哪一项，并且不写文件。

举例：螺纹钢每点 10 元、最小跳动 1 点、单笔上限 300 元，保存后 `value_per_tick` 自动变成 10。

如果 OCR 失败或当前不在 Windows，用「手工填价」输入合约、买一、卖一、最新价，后续检查逻辑完全相同。弹窗会自动带出上一次识别到的合约与价格，改错直接覆盖即可。

所有数值输入（手数、止损跳数、止盈跳数、入场/止损/止盈、手工填价里的三个价格）右侧都有上下箭头。

价格基数大的品种（如橡胶 19890）单点一次太慢，用「价格调整步长（跳）」控制：选 1 时每次动 1 跳（RU 是 5 点），选 10 时每次动 10 跳（50 点），可选 1 / 5 / 10 / 20。

止损、止盈两个价格框也可以直接键入数字；点「检查计划」时会按实际价差反算跳数，保证上面的「止损跳数 / 止盈跳数」与价格一致。

### 按 R 计算手数

在「止盈 = 止损 ×」下方有一行 1R 计算：

```text
1R 金额（元）[ 400 ]  [按 R 套用手数]  1R ¥400 ÷（25 跳 × ¥5）= ¥125/手 → 3.2 手 → 3 手（风险 ¥375，余 ¥25）
```

- 「④ 手数」区块第一行是「总资金」，右侧实时显示上限来源：固定金额显示「上限 ¥200（总资金不参与计算）」，百分比模式显示「上限 = ¥50,000 × 2% = ¥1,000」；总资金会记住，存在 `data/account.json`；
- 默认勾选「1R = 单笔上限」，直接取当前生效的单笔上限，此时 1R 输入框是禁用的；取消勾选才能填自定义预算（自定义值会被记住）；
- 自动显示每手风险、可开手数（向下取整）、实际占用和剩余；
- 点「按 R 套用手数」把手数填进表单并立即执行一次检查；
- 不足 1 手时提示「1R 不足 1 手」；
- 每手风险包含手续费与滑点缓冲，因此 1R 推导结果与右栏「计划风险」始终一致；
- 超过该品种单笔上限时显示 ⚠ 警告，但不阻止（上限可以在「品种配置」里调整）；
- 品种未启用风险配置时提示先配置。

1R 金额会被记住，下次打开表单沿用。

### 止盈按盈亏比联动

默认勾选「止盈 = 止损 ×」，右侧填倍数（x > 0，可选 1 / 1.5 / 2 / 3 / 4，也可直接输入任意正数）：

```text
止损 20 跳 × 2   → 止盈 40 跳（RU 为 200 点）
止损 30 跳 × 1.5 → 止盈 45 跳
```

调整止损（跳数或价格）时，止盈会自动跟着变；联动开启时「止盈跳数」框是禁用的，避免两处打架。取消勾选即可手动单独设止盈。倍数填 0 或负数会提示错误，不会生成计划。

界面同样只读：不点击 WH6、不提交订单。Tesseract 路径可在窗口底部修改，关闭时自动记住。

## 13. 日常运行建议

每笔交易前：

1. 捕获 WH6 当前行情；
2. 核对合约、买一、卖一、最新价；
3. 选择方向、价格模式、手数、SL；
4. 选择 SMC 标签和 HTF bias；
5. 执行交易前检查；
6. 保存计划快照；
7. 在 WH6 中人工确认下单。

每笔交易后：

1. 记录实际成交价和成交手数；
2. 记录实际平仓价和时间；
3. 记录手续费/滑点；
4. 标记是否加仓、拖损、提前止盈或偏离计划；
5. 运行周期报告。

每新增 20–30 笔交易：

- 检查纪律违规率；
- 检查加仓和超额造成的亏损；
- 检查各品种风险配置是否合理；
- 不自动修改规则；
- 形成候选规则，人工确认后再升级。

## 14. 安全边界

本工具面向仿真/研究辅助。它不接入券商、不读取账户资金、不自动下单。任何 OCR 识别错误都可能导致错误的交易计划，因此：

- 识别结果必须人工确认；
- 风险检查通过不代表交易值得执行；
- 未配置品种不应绕过拦截；
- 不要把未计手续费的风险数字当作完整成本；
- 不要把历史胜率当作未来保证；
- A/B 模型当前未实现，只做留档。
