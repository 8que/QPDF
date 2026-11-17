"""QReport —— 只读地读 QPDF 项目文件（``.qpdf``）的小工具。

只回答一个问题：**某个管线号相关的那几页，数字标注最大到几号？**

不依赖 Qt / PyMuPDF，只 import 标准库；对 ``.qpdf`` 全程只读。
"""

from __future__ import annotations

from qreport.reader import (
    KIND_NUMBER,
    NumberMark,
    QpdfError,
    QpdfReader,
    SkippedMark,
    TagReport,
)

__all__ = [
    "KIND_NUMBER",
    "NumberMark",
    "QpdfError",
    "QpdfReader",
    "SkippedMark",
    "TagReport",
]

__version__ = "1.3.1"
