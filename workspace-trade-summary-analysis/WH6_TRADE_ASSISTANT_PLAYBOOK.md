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

当前命令行版本需要一个交易计划 JSON。示例：

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

JD、RU、MA、C、JM 当前配置为未启用，检查会拒绝通过，直到补齐风险参数。

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

## 12. 日常运行建议

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

## 13. 安全边界

本工具面向仿真/研究辅助。它不接入券商、不读取账户资金、不自动下单。任何 OCR 识别错误都可能导致错误的交易计划，因此：

- 识别结果必须人工确认；
- 风险检查通过不代表交易值得执行；
- 未配置品种不应绕过拦截；
- 不要把未计手续费的风险数字当作完整成本；
- 不要把历史胜率当作未来保证；
- A/B 模型当前未实现，只做留档。
