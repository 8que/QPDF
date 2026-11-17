"""QReport 命令行：``python -m qreport <项目.qpdf> --tag <管线号>``。

退出码：0 正常；2 打不开/不是 QPDF 项目；3 项目里没找到这个管线号。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from qreport.reader import QpdfError, QpdfReader
from qreport.welds import weld_rows
from qreport.xlsx import weld_table_xlsx


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qreport",
        description="只读统计 QPDF 项目里某个管线号的数字标注（不碰原始 PDF）。",
    )
    parser.add_argument("qpdf", help="项目文件（.qpdf）路径")
    parser.add_argument("--tag", "-t", default="", help="管线号（例如 50-S0-P1302A01-N1B-H60）")
    parser.add_argument("--all", action="store_true", help="把所有管线号都算一遍")
    parser.add_argument("--list-tags", action="store_true", help="只列出项目里有哪些管线号")
    parser.add_argument("--no-hidden", action="store_true", help="跳过被隐藏的标注")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给别的程序吃）")
    parser.add_argument(
        "--xlsx",
        default="",
        metavar="文件.xlsx",
        help="导出「管线号 / 焊口号 / 页码 / 探伤比例」四列表格（给 --tag 就导一个，不给就全导）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="打包后自检：只读打开、数一下标注，打印一行结果后退出（不做统计）",
    )
    return parser


def _print_report(report, reader: QpdfReader) -> None:
    info = reader.source_info()
    print(f"项目文件：{info['path']}")
    if info["pdf"]:
        print(f"原始 PDF：{info['pdf']}（{info['page_count']} 页）")
    if report.tag:
        pages = "、".join(str(no) for no in report.page_nos) or "（无）"
        print(f"管线号　：{report.tag}")
        print(f"相关页面：{pages}（共 {len(report.pages)} 页）")
    print(f"数字标注：{len(report.marks)} 条可统计，{len(report.skipped)} 条跳过")
    max_value = report.max_value
    print(f"最大编号：{max_value if max_value is not None else '（没有可统计的整数标注）'}")
    if report.marks:
        print("明细：")
        for mark in report.marks:
            flag = "（已隐藏）" if mark.hidden else ""
            print(
                f"  第 {mark.page_no} 页  编号 {mark.value:<6} "
                f"组 {mark.seq_group or '-'}#{mark.seq_index if mark.seq_index is not None else '-'} "
                f"[{mark.source}]{flag}"
            )
    if report.skipped:
        print("跳过（没法算成整数编号）：")
        for item in report.skipped:
            print(f"  第 {item.page_no} 页  内容 {item.raw!r}  —— {item.reason}")


def _interactive() -> int:
    """没有参数时（多半是双击 exe）：一路问下来，别让窗口一闪就没。"""
    print("QReport —— 只读统计 .qpdf 里的数字标注")
    print("（只读：.qpdf 和原始 PDF 都不会被改动）\n")
    path = input("把 .qpdf 拖进来（或输入路径）：").strip().strip('"').strip("'")
    if not path:
        return 2
    try:
        with QpdfReader(path) as reader:
            info = reader.source_info()
            if info["pdf"]:
                print(f"原始 PDF：{info['pdf']}（{info['page_count']} 页）")
            tags = reader.tags()
            print(f"项目里有 {len(tags)} 个管线号。")
            tag = input(
                "输入管线号（直接回车 = 把所有编号各算一行；输入 ? 看前 20 个）："
            ).strip()
            if tag == "?":
                for item in tags[:20]:
                    print("  ", item)
                tag = input("输入管线号：").strip()
            if tag:
                report = reader.report_for_tag(tag)
                if not report.pages:
                    print(f"项目里没有「{tag}」的识别记录。")
                    return 3
                _print_report(report, reader)
            else:
                for name, rep in reader.report_all().items():
                    max_value = rep.max_value
                    print(
                        f"{name}\t{len(rep.pages)} 页\t{len(rep.marks)} 条\t"
                        f"最大 {max_value if max_value is not None else '-'}"
                    )
            return 0
    except QpdfError as exc:
        print(f"[QReport] {exc}")
        return 2


def _utf8_when_redirected() -> None:
    """输出被重定向到文件/管道时用 UTF-8。

    Windows 下 Python 只在**直接连控制台**时用 UTF-8（PEP 528）；重定向到文件时
    会退回系统的 GBK，别的程序（或 QReport 自己）按 UTF-8 读就成了乱码。
    控制台里保持原样，不影响现场看窗口。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 改不了就算了，不能因为输出编码崩掉
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_when_redirected()
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                return _interactive()
            except (EOFError, KeyboardInterrupt):
                print()
                return 2
        _build_parser().print_help()
        return 2
    args = _build_parser().parse_args(argv)
    try:
        with QpdfReader(args.qpdf) as reader:
            if args.selftest:
                info = reader.source_info()
                tags = reader.tags()
                numbers = reader.number_annotations()
                seqs = reader.sequences()
                print(
                    f"[自检通过] {os.path.basename(args.qpdf)}"
                    f" 页数 {info['page_count']} ｜ 管线号 {len(tags)} 个"
                    f" ｜ 数字标注 {len(numbers)} 条 ｜ 编号组 {len(seqs)} 组"
                )
                return 0

            if args.list_tags:
                tags = reader.tags()
                if args.json:
                    print(json.dumps(tags, ensure_ascii=False, indent=1))
                else:
                    print(f"共 {len(tags)} 个管线号：")
                    for tag in tags:
                        print(f"  {tag}")
                return 0

            if args.all:
                reports = reader.report_all(include_hidden=not args.no_hidden)
                if args.json:
                    payload = {tag: report.to_dict() for tag, report in reports.items()}
                    print(json.dumps(payload, ensure_ascii=False, indent=1))
                else:
                    for tag, report in reports.items():
                        max_value = report.max_value
                        print(
                            f"{tag}\t{len(report.pages)} 页\t{len(report.marks)} 条\t"
                            f"最大 {max_value if max_value is not None else '-'}"
                        )
                return 0

            if args.xlsx:
                wanted = args.tag.strip()
                rows = weld_rows(reader, wanted or None, include_hidden=not args.no_hidden)
                tags_found = {row.tag for row in rows}
                if wanted and wanted not in tags_found:
                    print(f"项目里没有「{wanted}」的识别记录。", file=sys.stderr)
                    return 3
                if not rows:
                    print("这个项目里还没有可导出的焊口号（先在图纸上标数字）。", file=sys.stderr)
                    return 3
                count = weld_table_xlsx(args.xlsx, rows)
                print(f"已导出 {count} 行、{len(tags_found)} 个管线号 -> {args.xlsx}")
                return 0

            tag = args.tag.strip()
            if not tag:
                print("要么给 --tag，要么用 --all / --list-tags。", file=sys.stderr)
                return 2
            report = reader.report_for_tag(tag, include_hidden=not args.no_hidden)
            if not report.pages:
                print(f"项目里没有「{tag}」的识别记录。", file=sys.stderr)
                return 3
            if args.json:
                print(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
            else:
                _print_report(report, reader)
            return 0
    except QpdfError as exc:
        print(f"[QReport] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
