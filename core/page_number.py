"""页码装饰层。

页码不是 ``Annotation``：它不进 ``.qpdf`` 的 annotations 表，也不参与
qreport 的数字/文字统计。它只是项目设置里的一个「每页都在同一个位置写一行字」
的规格，导出时再画进 PDF 内容流。

格式支持 ``{page}`` / ``{n}`` / ``{页码}`` 和 ``{total}`` / ``{总页数}``，
例如 ``第 {page} 页 / 共 {total} 页``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from core.annotation import AnnotationStyle
from core.region import Region

DEFAULT_TEMPLATE = "第 {page} 页"

_PAGE_TOKEN = re.compile(r"\{(page|n|页码)(?::([^}]+))?\}")
_TOTAL_TOKEN = re.compile(r"\{(total|总页数)(?::([^}]+))?\}")


def _format_number(value: int, spec: str | None) -> str:
    if not spec:
        return str(value)
    try:
        return format(value, spec)
    except (ValueError, TypeError):
        return str(value)


@dataclass(frozen=True)
class PageNumberSpec:
    """页码规格。区域是页面比例，所有页共用同一个位置。"""

    region: Region
    template: str = DEFAULT_TEMPLATE
    style: AnnotationStyle = field(default_factory=AnnotationStyle)
    enabled: bool = True

    def text_for(self, page: int, total: int) -> str:
        template = self.template or DEFAULT_TEMPLATE
        text = _PAGE_TOKEN.sub(
            lambda m: _format_number(int(page), m.group(2)), template
        )
        text = _TOTAL_TOKEN.sub(
            lambda m: _format_number(int(total), m.group(2)), text
        )
        # 用户只写了前缀/后缀、忘了占位符：把页码补在后面，别静默丢页码
        if text == template and not _PAGE_TOKEN.search(template):
            text = f"{template}{page}"
        return text

    def with_style(self, style: AnnotationStyle) -> "PageNumberSpec":
        return replace(self, style=style)

    def to_dict(self) -> dict:
        return {
            "region": self.region.to_dict(),
            "template": self.template,
            "style": self.style.to_dict(),
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, payload) -> "PageNumberSpec | None":
        if not isinstance(payload, dict):
            return None
        region = Region.from_dict(payload.get("region"))
        if region is None or region.is_empty:
            return None
        template = str(payload.get("template") or DEFAULT_TEMPLATE)
        style_payload = payload.get("style")
        style = (
            AnnotationStyle.from_dict(style_payload)
            if isinstance(style_payload, dict)
            else AnnotationStyle()
        )
        return cls(
            region=region,
            template=template,
            style=style,
            enabled=bool(payload.get("enabled", True)),
        )
