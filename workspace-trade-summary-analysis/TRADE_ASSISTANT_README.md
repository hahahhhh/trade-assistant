# 交易辅助引擎（第一阶段）

本阶段提供命令行/配置引擎，不操作交易软件、不提交订单。A/B 模型只留档，尚未实现。

## 使用方式

从工作区根目录运行：

```bash
python3 workspace-trade-summary-analysis/trade_assistant.py check /path/to/plan.json
```

也可指定配置、快照文件：

```bash
python3 workspace-trade-summary-analysis/trade_assistant.py check plan.json \
  --config workspace-trade-summary-analysis/data/trade_assistant_config.json \
  --snapshots workspace-trade-summary-analysis/data/trade_assistant/pre_trade_snapshots.jsonl
```

## WH6（mytrader_spqh.exe）截图识别

本地截图文件 OCR：

```bash
python3 workspace-trade-summary-analysis/wh6_screen.py --image /path/to/wh6.png
```

Windows 上捕获 WH6 进程的可见窗口并 OCR（只读，不模拟点击或下单）：

```powershell
py -m pip install pywin32 psutil Pillow pytesseract
# 另行安装 Tesseract OCR，并确保有 chi_sim + eng 语言包
py workspace-trade-summary-analysis/wh6_screen.py --capture
```

定时识别（每2秒），按 Ctrl+C 停止；也可以加 `--out market_snapshots.jsonl` 留档：

```powershell
py workspace-trade-summary-analysis/wh6_screen.py --capture --watch --interval 2 --out market_snapshots.jsonl
```

如需只 OCR 窗口内局部区域，可用相对截图左上角的 `--region x,y,width,height`。如 Tesseract 不在 PATH，用 `--tesseract-cmd 'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'` 指定。

识别策略针对 WH6 的图1右侧行情栏（合约、卖出/买入、最新、涨跌等字段）。图2下单面板中的买多/卖空/平仓按钮不会被点击，程序也不向原窗口写入。截图 OCR 结果会校验品种配置、最小跳动和买卖盘顺序；低置信度或不完整字段必须人工核验。

Windows 窗口捕获依赖指定进程 `mytrader_spqh.exe`。本项目运行环境如果不是 Windows，`--capture` 不可用；可在 Windows 上运行该脚本，或先从 WH6 截图再使用 `--image`。

## 计划 JSON 格式

```json
{
  "variety": "V",
  "contract": "V2701",
  "direction": "long",
  "size": 1,
  "price_mode": "opponent",
  "stop_loss": 4860,
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

字段枚举：

- `direction`: `long` / `short`；未来图形界面用“买入/多”“卖出/空”按钮。
- `price_mode`: `manual`、`opponent`、`queue`、`last`、`offset`。
- `smc_tag`: `FVG`、`OB`、`Sweep`、`Rejection`、`BOS`、`Break Retest`、`Liquidity`、`MSS/CHoCH`、`Other` 或自定义文本。
- `htf_bias`: `long`、`short`、`ranging`、`unclear`。
- 对手价：多单用卖一，空单用买一；排队价：多单用买一，空单用卖一。
- `offset` 可设置 `base_price` 和 `offset_ticks`；默认基准价是对手侧盘口。

## 风险计算

按最小跳动向上取整：

```text
总计划风险 = 跳数 × 每跳人民币价值 × 手数 + 费用/滑点缓冲
```

首版已配置 V、LH、FG，每品种单笔上限暂为 ¥200：

- V：¥5/点，最小跳动 1 点，40 点对应 ¥200；
- LH：¥16/点，最小跳动 5 点，每跳 ¥80；2 跳 ¥160，3 跳 ¥240；
- FG：¥20/点，最小跳动 1 点，10 点对应 ¥200。

JD、RU、MA、C、JM 尚未配置，因此检查会拒绝通过。请在交易前补齐各品种风险上限、每点价值、最小跳动、手续费/滑点后再启用。

注意：当前配置中的费用缓冲为 0，代表尚未录入真实期货公司手续费/滑点，风险结果是未计成本的下界，不可误认为完整净风险。

## 快照与 A/B 留档

每次 `check` 将追加保存交易前输入、检查结果、规则版本和 A/B 状态到 JSONL。默认路径：

`workspace-trade-summary-analysis/data/trade_assistant/pre_trade_snapshots.jsonl`

原始交易账本不修改。A/B 状态为 `not_implemented_archived_only`。

## 当前限制

- 日内 5 次开仓、日损失/总敞口等规则需要从已录入计划/持仓状态累计；当前 CLI 原型尚未接入完整状态机。
- `wh6_screen.py` 已提供截图 OCR 与 Windows 进程窗口捕获代码；当前开发环境未安装 OCR 依赖，尚未对 WH6 实机截图完成端到端验证。
- 计划 JSON 代表手工输入或 OCR 后人工确认的数据；暂不自动读取账户持仓，不执行点击/下单。
- `wh6_screen.py --capture` 只能在 Windows 运行；当前 Linux 开发环境仅验证了解析、校验和截图接口，未验证 Windows 窗口捕获。
- `daily_loss_limit_rmb`、`max_total_exposure_rmb` 是配置项，当前版本仅记录，完整持仓状态检查待接入。
