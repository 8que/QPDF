"""按界面字体族找 Windows 字体文件，供导出时真正嵌入用户选的字体。

只在需要把中文写进 PDF 内容流时用（目前是页码）。MuPDF 内置的 ``china-s``
只有一个中文字形，属性面板选「宋体/微软雅黑/楷体」时它不会跟着变；这里把
字体族映射到系统字体文件，交给 ``pymupdf.Font(fontfile=...)``，再靠导出末尾的
``subset_fonts()`` 只保留用到的字模。

找不到字体文件时返回空字符串，调用方继续用内置字体——不能因为字体缺失让导出失败。
"""
from __future__ import annotations

import os
from pathlib import Path

_FONT_DIR = Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"

#: 常见中文字体族 -> (常规文件, 粗体文件)。粗体文件不存在时退回常规文件。
#: TTC 是字体集合，pymupdf 取第一面；这几个系统的第一面正好就是我们要的字族。
_FAMILIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("yahei ui", "yahei", "微软雅黑"), "msyh.ttc", "msyhbd.ttc"),
    (("simsun", "nsimsun", "宋体", "songti", "stsong"), "simsun.ttc", "simsunb.ttf"),
    (("simhei", "黑体", "heiti"), "simhei.ttf", "simhei.ttf"),
    (("kaiti", "楷体", "楷"), "simkai.ttf", "simkai.ttf"),
    (("fangsong", "仿宋"), "simfang.ttf", "simfang.ttf"),
    (("dengxian", "等线"), "Deng.ttf", "Dengb.ttf"),
    (("lisu", "隶书"), "SIMLI.TTF", "SIMLI.TTF"),
    (("youyuan", "幼圆"), "SIMYOU.TTF", "SIMYOU.TTF"),
    (("jhenghei", "正黑"), "msjh.ttc", "msjhbd.ttc"),
)


def _first_existing(*names: str) -> str:
    for name in names:
        path = _FONT_DIR / name
        if path.is_file():
            return str(path)
    return ""


def font_file_for(family: str, bold: bool = False) -> str:
    """字体族名 -> 本机字体文件绝对路径；找不到返回 ``""``。"""
    name = (family or "").strip().lower()
    if not name:
        return ""
    for aliases, regular, bold_file in _FAMILIES:
        if any(alias in name for alias in aliases):
            if bold:
                found = _first_existing(bold_file, regular)
            else:
                found = _first_existing(regular)
            if found:
                return found
    return ""
