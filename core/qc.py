"""把一段识别结果里的"可疑字符"挑出来（Phase 10 校对用）。

只做**提示**，不改内容——用户看着这一列决定是改字、忽略这一条，
还是以后都不提示这一类。规则本身也存进项目（``ocr_rules``），
换台机器打开同一个项目仍然有效。
"""
from __future__ import annotations

import re

#: 管线号里允许出现的字符
ALLOWED = re.compile(r"^[A-Z0-9\-()\[\]/.#]+$")

KIND_CJK = "cjk"
KIND_FULLWIDTH = "fullwidth"
KIND_SPACE = "space"
KIND_PUNCT = "punct"
KIND_SHORT = "short"
KIND_LOWERCASE = "lower"
KIND_WEIRD = "weird"

KIND_LABELS = {
    KIND_CJK: "汉字",
    KIND_FULLWIDTH: "全角字符",
    KIND_SPACE: "夹杂空白",
    KIND_PUNCT: "中文标点",
    KIND_SHORT: "内容太短",
    KIND_LOWERCASE: "小写字母",
    KIND_WEIRD: "非常见符号",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
_FULLWIDTH = re.compile(r"[\uff01-\uff5e\u3000]")
_CJK_PUNCT = re.compile(
    r"[\u3001\u3002\uff0c\uff1b\uff1a\u201c\u201d\u3010\u3011\u300a\u300b\u00b7\u2022\u30fb]"
)


def empty_rules() -> dict:
    return {"ignore_kinds": [], "ignore_chars": ""}


def load_rules(payload) -> dict:
    """把项目里存的规则规整成 ``{"ignore_kinds": [...], "ignore_chars": "..."}``。"""
    rules = empty_rules()
    if isinstance(payload, dict):
        kinds = payload.get("ignore_kinds") or []
        if isinstance(kinds, str):
            kinds = [kinds]
        rules["ignore_kinds"] = [str(k) for k in kinds if str(k) in KIND_LABELS]
        rules["ignore_chars"] = str(payload.get("ignore_chars") or "")
    return rules


def _ignored_chars(rules: dict | None) -> set:
    if not rules:
        return set()
    return set(str(rules.get("ignore_chars") or ""))


def find_suspects(text: str, rules: dict | None = None) -> list:
    """返回人话形式的可疑点，例如 ``['汉字「一」', '全角字符「（」']``。"""
    raw = str(text or "")
    if not raw.strip():
        return []
    ignore_kinds = set((rules or {}).get("ignore_kinds") or [])
    ignored = _ignored_chars(rules)
    found: list = []
    seen: set = set()

    def add(kind: str, ch: str) -> None:
        if kind in ignore_kinds:
            return
        label = KIND_LABELS[kind]
        if ch:
            label = label + "「" + ch + "」"
        if label not in seen:
            seen.add(label)
            found.append(label)

    for ch in raw:
        if ch in ignored:
            continue
        if _CJK.match(ch):
            add(KIND_CJK, ch)
        elif _CJK_PUNCT.match(ch):
            add(KIND_PUNCT, ch)
        elif _FULLWIDTH.match(ch):
            add(KIND_FULLWIDTH, ch)
        elif ch.isspace() and ch != " ":
            add(KIND_SPACE, "")
        elif ch.isalpha() and ch.islower():
            add(KIND_LOWERCASE, ch)
        elif not ch.isalnum() and ch not in "-()[]/.# ":
            add(KIND_WEIRD, ch)

    compact = re.sub(r"[\s\u3000]+", "", raw)
    if compact and len(compact) < 6:
        add(KIND_SHORT, "")
    return found


def char_candidates(text: str, rules: dict | None = None) -> list:
    """可以"以后不再提示"的具体字符（给右键菜单用）。"""
    out: list = []
    for ch in str(text or ""):
        if ch.isalnum() or ch in "-()[]/.# ":
            continue
        if ch not in out:
            out.append(ch)
    return out
