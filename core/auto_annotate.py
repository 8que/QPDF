"""把"识别结果 + 表格匹配"变成"要写哪一页哪个位置、写什么"（第 5~8 步）。

这一层刻意不碰 Qt 窗口和 PDF：

- 给对话框算预览行、算统计；
- 生成 ``Annotation`` 对象（用的是雀雀 PDF 自己的标准文字标注）；
- 算"重新匹配后变了哪些"，供变更预览用。

坐标仍然只用页面坐标。标注区域是页面比例（``Region``），写进去之前换算成
这一页的 pt；屏幕像素一概不参与。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from core import dataset
from core.annotation import (
    ALIGN_CENTER,
    ALIGN_RIGHT,
    KIND_TEXT,
    Annotation,
    AnnotationStyle,
    new_id,
)
from core.region import Region

#: 预览表里的状态（和 dataset 的匹配状态不是一回事：这里还带"没识别到"）
ST_MATCHED = "matched"      # 唯一命中，可以生成
ST_CONFLICT = "conflict"    # 表里多条，必须选
ST_UNMATCHED = "unmatched"  # 表里没有（可能给候选）
ST_NOVALUE = "novalue"      # 命中了，但内容字段是空的
ST_EMPTY = "empty"          # 这一页没识别出管线号
ST_SKIP = "skip"            # 用户手动跳过
ST_DUP = "dup"              # 同名管线号，按设置跳过（可在表里勾回来）
ST_MANUAL = "manual"        # 表里没有，但人自己填了字（他确认过要这么写）

LABELS = {
    ST_MATCHED: "已匹配",
    ST_CONFLICT: "记录冲突",
    ST_UNMATCHED: "未匹配",
    ST_NOVALUE: "字段为空",
    ST_EMPTY: "未识别",
    ST_SKIP: "已跳过",
    ST_DUP: "同名跳过",
    ST_MANUAL: "人工填字",
}

#: 能批量生成标注的状态（人工填字的也允许，但生成前会提醒）
GENERATABLE = (ST_MATCHED, ST_MANUAL)


@dataclass
class PlanRow:
    """预览表的一行 = 一页的处置方案。"""

    page: int
    raw: str = ""
    tag: str = ""
    status: str = ST_EMPTY
    content: str = ""
    note: str = ""
    source: str = ""
    fixed: bool = False
    record: dict = field(default_factory=dict)
    candidates: list[tuple[str, float]] = field(default_factory=list)
    use: bool = True
    #: 人是不是动过这一行（手选记录 / 手改内容），生成时要在备注里留痕
    manual: bool = False
    #: 被「同名只标一次」跳过之前的真实状态，取消设置时要还原
    base_status: str = ""
    #: 同名管线号第一次出现在哪一页（1 基之外，这里存 0 基页号，-1 表示没被跳过）
    dup_of: int = -1
    #: 这次查表实际用的键（整串或中间两段），手选记录的搜索词就用它
    lookup_key: str = ""
    #: 这个键是从整串 / 中间两段切出来的
    via: str = ""
    #: 内容是人手改的（双击改字），生成前要提醒
    edited: bool = False
    #: 内容字段本来是空的，写的是用户设的默认字
    placeholder: bool = False

    @property
    def label(self) -> str:
        return LABELS.get(self.status, self.status)

    @property
    def ready(self) -> bool:
        return bool(self.use and self.content and self.status in GENERATABLE)


def status_of(match: dataset.MatchResult, content: str = "") -> str:
    if match.status == dataset.MATCHED:
        return ST_MATCHED if content else ST_NOVALUE
    if match.status == dataset.CONFLICT:
        return ST_CONFLICT
    if match.status == dataset.NOVALUE:
        return ST_NOVALUE
    return ST_UNMATCHED


def plan_from_scans(
    scans: list[dict], records: dataset.RecordTable, empty_text: str = ""
) -> list[PlanRow]:
    """``scans`` 是识别线程回传的行（page/raw/tag/source/fixed/error）。

    ``empty_text``：内容字段是空的时改写的字（用户自己设的）；留空就还是"字段为空"。
    """
    plan: list[PlanRow] = []
    for scan in scans:
        row = PlanRow(
            page=int(scan.get("page", 0)),
            raw=scan.get("raw", "") or "",
            tag=(scan.get("tag") or "").strip(),
            source=scan.get("source", "") or "",
            fixed=bool(scan.get("fixed")),
            note=(scan.get("error") or "").strip(),
        )
        if not row.tag:
            row.status = ST_EMPTY
            plan.append(row)
            continue
        match = records.lookup(row.tag)
        row.candidates = match.candidates
        row.lookup_key = match.key
        row.via = match.via
        if match.via == dataset.KEY_MIDDLE and match.key:
            row.note = _join(row.note, f"按中间两段匹配（{match.key}）")
        if match.status == dataset.CONFLICT:
            row.status = ST_CONFLICT
            row.record = {}
            row.note = _join(row.note, f"表里有 {len(match.rows)} 条同名记录，必须选一条")
        elif match.status == dataset.MATCHED:
            row.record = match.rows[0]
            row.content, state = records.content_for(row.record)
            if state == dataset.NOVALUE and empty_text:
                row.content = empty_text
                row.placeholder = True
                row.status = ST_MATCHED
                row.note = _join(row.note, f"表格里字段为空，写了「{empty_text}」")
            else:
                row.status = ST_MATCHED if state == dataset.MATCHED else ST_NOVALUE
            if row.fixed:
                row.note = _join(row.note, "OCR 已自动修正，且命中表记录")
        else:
            row.status = ST_UNMATCHED
            row.note = _join(row.note, "表里没有这个编号（不自动代填）")
        plan.append(row)
    return plan


def _join(*parts: str) -> str:
    return "；".join(part for part in parts if part)


def apply_duplicates(rows: list[PlanRow], skip: bool) -> int:
    """同名管线号只标一次（默认关：每页都标）。

    只拿"本来能生成"的行去重——第一页未匹配、第二页匹配上的情况，
    第二页当然要标，不能因为同名就把能标的丢掉。返回这次跳过了几行。
    """
    seen: dict[str, int] = {}
    skipped = 0
    for row in rows:
        if row.status == ST_DUP and not skip:
            # 设置被关掉：还原成原来的状态
            row.status = row.base_status or ST_MATCHED
            row.base_status = ""
            row.dup_of = -1
            row.use = True
            row.note = ""
            continue
        if not skip or row.status != ST_MATCHED:
            continue
        key = (row.tag or "").upper()
        if not key:
            continue
        first = seen.get(key)
        if first is None:
            seen[key] = row.page
            continue
        row.base_status = row.status
        row.status = ST_DUP
        row.use = False
        row.dup_of = first
        row.note = _join(row.note, f"与第 {first + 1} 页同名，按设置跳过")
        skipped += 1
    return skipped


def unskip_duplicate(row: PlanRow) -> None:
    """用户在预览里把「同名跳过」那行重新勾上：按原状态恢复成可生成。"""
    if row.status != ST_DUP:
        return
    row.status = row.base_status or ST_MATCHED
    row.base_status = ""
    row.dup_of = -1
    row.use = True
    row.note = _join("手动勾选（同名也标）")


def apply_choice(
    row: PlanRow, records: dataset.RecordTable, record: dict | None
) -> PlanRow:
    """用户在预览里手动指定了记录：状态改回"已匹配"，并记下这是人手选的。"""
    if record is None:
        row.record = {}
        row.content = ""
        row.status = ST_UNMATCHED
        row.manual = True
        row.note = "已取消匹配"
        return row
    row.record = record
    row.content, state = records.content_for(record)
    row.status = ST_MATCHED if state == dataset.MATCHED else ST_NOVALUE
    row.manual = True
    row.note = _join("手选记录", "" if state == dataset.MATCHED else "内容字段为空")
    return row


# --------------------------------------------------------------------- 生成


def anchor(region: Region, page_width: float, page_height: float, align: str, pad: float) -> tuple[float, float]:
    """标注区域里的落笔点（页面坐标）。

    文字标注的锚点就是文本框的左边/上边：左对齐在左边、居中在中间、
    右对齐在右边；纵向统一从顶边往下留 ``pad``。
    """
    x0, y0, x1, _y1 = region.to_pdf(page_width, page_height)
    if align == ALIGN_CENTER:
        x = (x0 + x1) / 2.0
    elif align == ALIGN_RIGHT:
        x = x1 - pad
    else:
        x = x0 + pad
    return x, y0 + pad


def final_text(
    row: PlanRow,
    prefix: str = "",
    suffix: str = "",
    decorate_placeholder: bool = True,
) -> str:
    """真正写到图上的字。

    - 没有内容（未匹配 / 没识别 / 字段为空）就是空，不许把前后缀孤零零写上去；
    - 手工改过的原样用；
    - 默认字（表里空的、写了用户定的字）可以选择不套前后缀。
    """
    if not row.content:
        return ""
    if row.edited:
        return row.content
    if row.placeholder and not decorate_placeholder:
        return row.content
    if not prefix and not suffix:
        return row.content
    return f"{prefix}{row.content}{suffix}"


def fit_text_size(text: str, max_width: float, font_size: float, measure, floor: float = 4.0) -> float:
    """字太长就缩字号，别冲出一长条到框外面去。

    ``measure(text, size) -> width``（UI 那边用 QFontMetrics 量，核心不碰 Qt）。
    """
    if not text or max_width <= 0 or measure is None:
        return font_size
    size = float(font_size)
    if measure(text, size) <= max_width:
        return size
    while size > floor:
        size = max(floor, size - 0.5)
        if measure(text, size) <= max_width:
            return size
    return floor


def make_annotations(
    rows: list[PlanRow],
    page_sizes: list,
    region: Region,
    style: AnnotationStyle,
    pad: float = 2.0,
    prefix: str = "",
    suffix: str = "",
    decorate_placeholder: bool = True,
    measure=None,
) -> list[tuple[PlanRow, Annotation]]:
    """把可生成的行变成标注对象；页码越界或页面尺寸缺失的跳过。

    ``page_sizes`` 是 ``[(宽, 高), …]``（项目里存的那份），按页取。
    ``prefix`` / ``suffix`` 是用户设的「匹配的字前面/后面再加点字」。
    """
    made: list[tuple[PlanRow, Annotation]] = []
    for row in rows:
        if not row.ready:
            continue
        if not (0 <= row.page < len(page_sizes)):
            continue
        size = page_sizes[row.page]
        if not size or len(size) < 2:
            continue
        width, height = float(size[0]), float(size[1])
        text = final_text(row, prefix, suffix, decorate_placeholder)
        if not text:
            # 没内容就别凭空落一条空标注
            continue
        x, y = anchor(region, width, height, style.align, max(0.0, float(pad)))
        used = style
        if measure is not None:
            room = max(1.0, region.width * width - 2.0 * max(0.0, float(pad)))
            fitted = fit_text_size(text, room, style.font_size, measure)
            if abs(fitted - style.font_size) > 1e-6:
                used = replace(style, font_size=fitted)
        ann = Annotation(
            id=new_id(),
            kind=KIND_TEXT,
            page=int(row.page),
            x=x,
            y=y,
            content=text,
            style=used,
        )
        made.append((row, ann))
    return made


def provenance(made: list[tuple[PlanRow, Annotation]]) -> list[dict]:
    """生成追溯记录：原始编号、匹配到的记录、内容、人手改没改过。"""
    out: list[dict] = []
    for row, ann in made:
        note = row.note
        if row.edited:
            note = _join(note, "内容是人手改的")
        if row.placeholder:
            note = _join(note, "表格里字段为空，写的是设置的默认字")
        out.append(
            {
                "annotation_id": ann.id,
                "page": int(row.page),
                "raw": row.raw,
                "tag": row.tag,
                "status": "manual" if (row.manual or row.edited) else "auto",
                "record": dict(row.record),
                "content": ann.content,
                "note": note,
            }
        )
    return out


def manual_rows(
    plan: list[PlanRow],
    prefix: str = "",
    suffix: str = "",
    decorate_placeholder: bool = True,
) -> list[tuple[PlanRow, str]]:
    """生成前要提醒用户的那些行：``(行, 要写的字)``。"""
    out: list[tuple[PlanRow, str]] = []
    for row in plan:
        if not row.ready:
            continue
        if row.edited or row.manual or row.placeholder:
            out.append((row, final_text(row, prefix, suffix, decorate_placeholder)))
    return out


# --------------------------------------------------------------------- 变更


@dataclass
class Change:
    """重新匹配后的一条差异。"""

    kind: str  # add / change / remove
    page: int
    old: str = ""
    new: str = ""
    tag: str = ""

    @property
    def label(self) -> str:
        return {"add": "新增", "change": "内容变化", "remove": "记录消失"}.get(
            self.kind, self.kind
        )


def diff(
    existing: dict[int, dict],
    plan: list[PlanRow],
    prefix: str = "",
    suffix: str = "",
    decorate_placeholder: bool = True,
) -> list[Change]:
    """``existing`` 是 ``{页号: {content, tag}}``（已经生成过的数据标注）。

    ``prefix`` / ``suffix`` 必须和生成时一致，否则前后缀一改就全是"内容变化"。
    """
    wanted = {row.page: row for row in plan if row.ready}
    changes: list[Change] = []
    for page, row in sorted(wanted.items()):
        text = final_text(row, prefix, suffix, decorate_placeholder)
        old = existing.get(page)
        if old is None:
            changes.append(Change("add", page, "", text, row.tag))
        elif (old.get("content") or "") != text:
            changes.append(Change("change", page, old.get("content") or "", text, row.tag))
    for page, old in sorted(existing.items()):
        if page not in wanted:
            changes.append(Change("remove", page, old.get("content") or "", "", old.get("tag") or ""))
    return changes


def summarize(plan: list[PlanRow]) -> dict[str, int]:
    counts = {
        "total": len(plan),
        "ready": sum(1 for row in plan if row.ready),
        ST_MATCHED: 0,
        ST_CONFLICT: 0,
        ST_UNMATCHED: 0,
        ST_NOVALUE: 0,
        ST_EMPTY: 0,
        ST_SKIP: 0,
    }
    for row in plan:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts
