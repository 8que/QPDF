# QReport —— 只读读 .qpdf 的统计模块

> 一句话：**给一个管线号，回答"相关的这几页上，数字标注最大到几号"**。
> 只读 `.qpdf`，不碰原始 PDF，不依赖 Qt / PyMuPDF（只 import 标准库）。

---

## 1. 为什么单独做一层

"最大编号"这件事**不能读标注的 `content`**。QPDF 的数字标注只存
`(seq_group, seq_index)`，屏幕上显示的编号是 `start + step × index` 现算出来的；
插队之后后面的编号全变了，而 `content` 还是建标注那一刻的快照。所以：

```
真实编号 = meta.sequences 里该组的 start + step × seq_index
```

这一层就是把这条规则单独实现好，以后做 QReport 的界面（F01~F09）直接调它，
不要再在界面里重写一遍。

---

## 2. 规则（实现里写死的口径）

| 规则 | 说明 |
|---|---|
| 只读 | SQLite `mode=ro` + `PRAGMA query_only`；不建表、不写 meta、不生成 `-wal/-shm`。WAL 库被 QPDF 开着打不开时，**复制到临时目录**再读，原件一字节不动。 |
| 按管线号取页 | `scan_results.tag = ?`，页号返回 **0 基**（§4 输出时转成 1 起）。 |
| 只看数字标注 | `annotations.kind = 'number'`；文字标注不参与。 |
| 编号要还原 | 优先用 `(seq_group, seq_index)` 按 `sequences` 现算；算不了再退回 `content`（老项目 / 手改过库）。 |
| 只保留纯整数 | `^-?\d+$`。`12a`、`1.5`、`1,000`、空值都算"没法统计"。 |
| 排除百分比 | `content` 里含 `%` 或 `％` 的一律跳过，原因写"带百分号（不是编号）"。 |
| 跳过项单独列 | 百分比、非整数、编号组丢了且内容也不是整数……都进 `skipped`，带原因，不混进最大值。 |
| 隐藏标注 | 默认一起算；`include_hidden=False` 可以排除（界面上"隐藏"通常只是不想看，不是不要统计）。 |

---

## 3. 用法

### 命令行

```powershell
# 看一个编号
.\.venv\Scripts\python.exe -m qreport "文档\雀雀PDF\项目\某图纸-b5ea7ebb.qpdf" --tag 50-S0-P1302A01-N1B-H60

# 有哪些编号（项目里识别出来的）
.\.venv\Scripts\python.exe -m qreport "xxx.qpdf" --list-tags

# 所有编号各算一份，一行一个（表格化）
.\.venv\Scripts\python.exe -m qreport "xxx.qpdf" --all

# 给别的程序吃
.\.venv\Scripts\python.exe -m qreport "xxx.qpdf" --tag 50-... --json
```

退出码：`0` 正常、`2` 打不开/不是 QPDF 项目、`3` 项目里没有这个管线号。

输出长这样（真实数据：`tests/out/case1933/1933.qpdf`，2712 页的图纸）：

```
项目文件：…\1933.qpdf
原始 PDF：…\1933.pdf（2712 页）
管线号　：100-BU-V110501-N1B-C50
相关页面：1（共 1 页）
数字标注：4 条可统计，0 条跳过
最大编号：7
明细：
  第 1 页  编号 4      组 s1#3 [sequence]
  第 1 页  编号 5      组 s1#4 [sequence]
  第 1 页  编号 6      组 s1#5 [sequence]
  第 1 页  编号 7      组 s1#6 [sequence]
```

### 当模块用

```python
from qreport import QpdfReader

with QpdfReader(r"D:\…\某图纸-b5ea7ebb.qpdf") as reader:
    report = reader.report_for_tag("50-S0-P1302A01-N1B-H60")
    print(report.max_value)          # 最大编号，没有可统计的就是 None
    for mark in report.marks:        # NumberMark：page_no / value / seq_group / seq_index / source
        print(mark.page_no, mark.value)
    for item in report.skipped:      # SkippedMark：page_no / raw / reason
        print("跳过", item.page_no, item.raw, item.reason)
    print(report.to_dict())          # 给界面/JSON 用的结构
```

`report.to_dict()` 的字段：

```json
{
  "tag": "50-S0-…",
  "pages": [51],
  "count": 3,
  "max": 3,
  "marks": [{"id": "…", "page": 51, "value": 1, "group": "s1", "index": 0,
             "source": "sequence", "hidden": false, "x": 200.0, "y": 150.0}],
  "skipped": [{"id": "…", "page": 51, "raw": "50%", "reason": "带百分号（不是编号）"}]
}
```

其他 API：`tags()`、`pages_for_tag(tag)`、`scan_rows()`、`number_annotations(pages)`、
`value_of(row)`、`report_all()`、`source_info()`、`sequences()`。

---

## 4. 验收

`tests/test_qreport.py`：**32 项**，覆盖

- 按编号取页（0 基 / 1 起的换算）；不存在的编号返回空表；
- 编号还原：`s2#0 = 起始 100`（content 是 "100"）、`s1#0 = 1`；
- 百分比跳过、非整数跳过、编号组丢了但内容是整数时兜底（`source="content"`）；
- 只有 TAG_A 的页参与统计，TAG_B 的页不会混进来；
- `include_hidden=False` 时只少掉隐藏的那条，不连坐；
- **只读**：读取前后 mtime/大小不变，目录里不新增任何文件；
- 坏库（不是 QPDF 项目）抛 `QpdfError`；
- 命令行 `python -m qreport --json` 的退出码与 JSON。

跑法：`.\.venv\Scripts\python.exe tests\test_qreport.py`

---

## 4.5 绿色版（打包好的 exe）

QReport 也有一个**单文件**绿色版，和 QPDF 完全独立（不需要 Python、不需要 Qt）：

```
dist\QReport_绿色版_v1.1.0.zip     （解压后是一个目录，整个拷走）
  └── QReport\
      ├── QReport.exe       双击开窗口；也可以拖 .qpdf 上去
      ├── _internal\        ← 不能删
      └── QReport说明.md
```

用法：

```powershell
# 双击 QReport.exe：会一路问你 .qpdf 路径和管线号
# 命令行：
QReport.exe "某图纸-b5ea7ebb.qpdf" --tag 50-S0-P1302A01-N1B-H60
QReport.exe "某图纸-b5ea7ebb.qpdf" --list-tags
QReport.exe "某图纸-b5ea7ebb.qpdf" --all
QReport.exe "某图纸-b5ea7ebb.qpdf" --tag 50-... --json > 结果.json
```

说明：

- 输出重定向到文件/管道时是 **UTF-8**，直接用 `>` 存 `--json` 不会乱码；
  在控制台窗口里看仍然按系统编码正常显示中文。
- 自检：`QReport.exe --selftest 某图纸.qpdf`，打一行"页数 / 管线号数 / 数字标注数 / 编号组数"。
- 重新打包：`powershell -ExecutionPolicy Bypass -File .\build_qreport.ps1`
  （产物在 `dist\QReport\` 和 `dist\QReport_绿色版_v1.1.0.zip`）。

## 4.6 可视化界面（1.1.0 起）

绿色版现在是有界面的：左边挑管线号，右边直接看统计。

- **打开**：点「打开项目文件…」，或者把 `.qpdf` 直接拖进窗口；
- **左边**：项目信息（原始 PDF、页数、保存时间）、搜索框、管线号列表（每行右边带「N 页」）；
- **右边**：选中的编号 + 相关页面、「最大编号」大卡、三个统计格（相关页面 / 可统计 / 跳过）、
  三段表格「明细 / 跳过项 / 全部编号」；
- **导出**：明细导出 CSV（`utf-8-sig`，Excel 双击不乱码）或 JSON；
- **深色模式**：右上角太阳/月亮切换，记住了下次还用这个；
- **全部编号**：第一次点开时把所有编号算一遍（2712 页、2363 个编号约 0.3 秒），
  双击一行跳回左边那个编号。

界面同样**只读**：`.qpdf` 与原始 PDF 都不会被改；配置写在程序目录
`config/qreport.ini`，不碰注册表。

命令行（`--cli`）仍然保留，给脚本用：

```powershell
QReport.exe --cli "某图纸-b5ea7ebb.qpdf" --tag 50-S0-P1302A01-N1B-H60
QReport.exe --cli "某图纸-b5ea7ebb.qpdf" --all --json > 全部编号.json
```

## 4.7 导出「管线号 / 焊口号」Excel（1.2.0 起）

给的是四列表：**管线号 | 焊口号 | 页码 | 探伤比例**，表头合并两行、宋体、列宽和示例一致，
双击就能打开。页码是该管线号出现的**全部页面**（1 起），多页用顿号连起来（如 `272、273`）；
探伤比例（`20%`）取自 QPDF 的数据匹配标注，没有就是空的。

排序规则（这一步顾客最容易挑毛病，先说清楚）：

1. **管线号**按它**第一次出现的 PDF 页码**排（页码小的在前），不按字母；
   带页号后缀的编号（`…-（06）`）**原样保留、不合并**；
2. 同一个管线号里，**焊口号从小到大**；
3. 同一个数字在几页上重复出现，只留一行；
4. **每个管线号至少一行**：没标焊口的，焊口号留空、页码照列、探伤比例照写；
   既没焊口也没比例的（多半是识别噪声）**也照原样列出**——现场要的是一个都不能少，
   多几行空行无所谓（`include_empty=False` 可以只留有内容的）；
5. 只写能算成整数的焊口号（百分比、脏内容不进 B 列，但百分比会进 D 列「探伤比例」）；
6. 默认跳过「隐藏」的焊口标注，`include_hidden=True` 可以带上。

三种用法：

```powershell
QReport.exe "某图纸.qpdf" --xlsx "焊口号.xlsx"               # 整本
QReport.exe "某图纸.qpdf" --tag 80-CWR-E120601-M1B --xlsx "一个管线号.xlsx"
QReport.exe --cli "某图纸.qpdf" --xlsx "焊口号.xlsx"        # 命令行（脚本用）
```

界面上是标题栏右边的 **导出 Excel** 按钮（蓝色那个），导的是**整本**。

实现上没有引 openpyxl：`qreport/xlsx.py` 手写最小 xlsx（zip + 几段 XML），
所以打包体积不变、也不多一个依赖；写盘先落 `.qreport-tmp` 再改名，失败不会留下半个文件。

## 5. QReport 后续功能（按你给的优先级）

| 编号 | 功能 | 优先级 | 现状 |
|---|---|---|---|
| F07 | 读取数字注释辅助统计（本模块 + `--all`） | P1 | ✅ 模块与命令行已完成 |
| F01 | 导入 PDF、读取书签 | P0 | 未做（QPDF 侧已有 `core/toc.py` 可复用） |
| F02 | 导入 Excel/CSV | P0 | 未做（QPDF 侧 `core/dataset.py` 已是自读 xlsx，可直接复用） |
| F03 | 匹配管线号与拍片比例 | P0 | 未做（QPDF 侧 `core/dataset.py` + `auto_annotate.py` 的匹配逻辑可复用） |
| F04 | 手动填写焊口数量 | P0 | 未做（界面 + 数据模型） |
| F05 | 根据焊口数量生成多行数据 | P0 | 未做（数据模型 + 表格） |
| F06 | 导出 Excel | P0 | 未做（QPDF 侧没有写 xlsx，需要新增写入器） |
| F08 | 自定义 Excel 模板 | P2 | 未做 |
| F09 | 历史报量记录 | P2 | 未做 |

实现建议：QReport 的主数据（项目、书签、编号匹配）尽量复用 QPDF 的 `core/`，
**界面另起**（`qreport_app/`），不要往 `ui/main_window.py` 里塞——那是 QPDF 标注工具的窗口。

---

## 6. 已知边界

1. **OCR 噪声会混进编号列表**：真实项目里能见到 `)-BU-PI107D02-N1B-C40-(0：` 这种
   带括号/冒号的识别结果。QReport 不做清洗（那属于 F03 匹配阶段的事），
   `--list-tags` 会原样列出来。
2. **百分比判断看的是 `content`**：如果一条数字标注的 content 被手改成 `50%`，
   它会被跳过——这是"排除百分比批注"要求的行为。
3. **跨组编号不连续**：最大值只反映"这个管线号相关页上出现过的编号"，
   不保证和别的管线号连号——这是 QPDF 的编号语义（整份文档一个序列），不是 bug。
