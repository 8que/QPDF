"""识别区域：在图纸上框一小块地方，之后每一页都从这里读字。

坐标按**页面比例**存（0~1），不存点数。原因很实际：一套图纸里 A1、A3 混排很常见，
管线号框在 A1 上是 (240, 1180)，换到 A3 页面上就得换算；存成比例之后，
无论页面多大，框的是"左上角往里 12%~28%、往下 70%~74%"这同一块位置，天然对得上。

（前提是这套图的框位置固定——用户说的正是这种"管线号在页面同一个位置"的场景。
万一以后要支持"每页位置不同"，再按页号建例外表即可，存储格式已经能扩。）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    """一块矩形区域，四个值都是页面尺寸的比例（0~1）。"""

    x0: float
    y0: float
    x1: float
    y1: float
    label: str = "识别区域"

    def normalized(self) -> "Region":
        """保证 x0<x1、y0<y1 且全部夹在 0~1 之内。"""
        x0, x1 = sorted((self.x0, self.x1))
        y0, y1 = sorted((self.y0, self.y1))
        clamp = lambda v: max(0.0, min(1.0, v))  # noqa: E731
        return Region(clamp(x0), clamp(y0), clamp(x1), clamp(y1), self.label)

    @property
    def is_empty(self) -> bool:
        return self.width <= 1e-6 or self.height <= 1e-6

    @property
    def width(self) -> float:
        return abs(self.x1 - self.x0)

    @property
    def height(self) -> float:
        return abs(self.y1 - self.y0)

    def to_pdf(self, page_width: float, page_height: float) -> tuple[float, float, float, float]:
        """换算成这一页的 PDF 坐标（左上角原点，y 向下，与屏幕一致）。"""
        r = self.normalized()
        return (
            r.x0 * page_width,
            r.y0 * page_height,
            r.x1 * page_width,
            r.y1 * page_height,
        )

    @classmethod
    def from_pdf(
        cls,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        page_width: float,
        page_height: float,
        label: str = "识别区域",
    ) -> "Region":
        if page_width <= 0 or page_height <= 0:
            return cls(0.0, 0.0, 0.0, 0.0, label)
        return cls(
            x0 / page_width, y0 / page_height, x1 / page_width, y1 / page_height, label
        ).normalized()

    def to_dict(self) -> dict:
        return {
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, payload) -> "Region | None":
        if not isinstance(payload, dict):
            return None
        try:
            region = cls(
                float(payload["x0"]),
                float(payload["y0"]),
                float(payload["x1"]),
                float(payload["y1"]),
                str(payload.get("label") or "识别区域"),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return region.normalized()

    def describe(self) -> str:
        r = self.normalized()
        return (
            f"{r.label}：左 {r.x0 * 100:.1f}% · 上 {r.y0 * 100:.1f}% · "
            f"右 {r.x1 * 100:.1f}% · 下 {r.y1 * 100:.1f}%"
        )
