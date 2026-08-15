"""Key parity across the en/fa/ru trees in app/static/js/i18n.js.

`renderRestoreDbInfoCard` reads `I18N[lang].restore.dbInfo` directly instead of
going through `t()`, so a key missing from fa/ru throws at runtime rather than
falling back. These tests catch that before it ships.

The JS object literal is parsed here in pure Python so the suite stays runnable
without node.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

I18N_JS = ROOT / "app" / "static" / "js" / "i18n.js"
LANGS = ("en", "fa", "ru")
BASE_LANG = "en"
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


class _JsLiteralParser:
    """Recursive-descent reader for the subset of JS used by the I18N literal."""

    ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}

    def __init__(self, text: str, pos: int = 0):
        self.s = text
        self.i = pos

    def _skip(self) -> None:
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif self.s.startswith("//", self.i):
                nl = self.s.find("\n", self.i)
                self.i = len(self.s) if nl < 0 else nl + 1
            elif self.s.startswith("/*", self.i):
                end = self.s.find("*/", self.i)
                self.i = len(self.s) if end < 0 else end + 2
            else:
                return

    def value(self):
        self._skip()
        c = self.s[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "'\"`":
            return self.string()
        return self.scalar()

    def obj(self) -> dict:
        self.i += 1  # '{'
        out: dict = {}
        while True:
            self._skip()
            if self.s[self.i] == "}":
                self.i += 1
                return out
            key = self.key()
            self._skip()
            if self.s[self.i] != ":":
                raise AssertionError(f"expected ':' after key {key!r} at offset {self.i}")
            self.i += 1
            out[key] = self.value()
            self._skip()
            if self.s[self.i] == ",":
                self.i += 1

    def arr(self) -> list:
        self.i += 1  # '['
        out: list = []
        while True:
            self._skip()
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self._skip()
            if self.s[self.i] == ",":
                self.i += 1

    def key(self) -> str:
        if self.s[self.i] in "'\"`":
            return self.string()
        start = self.i
        while self.s[self.i] not in " \t\r\n:":
            self.i += 1
        return self.s[start:self.i]

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        buf: list[str] = []
        while True:
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1]
                buf.append(self.ESCAPES.get(nxt, nxt))
                self.i += 2
                continue
            if c == quote:
                self.i += 1
                return "".join(buf)
            buf.append(c)
            self.i += 1

    def scalar(self):
        start = self.i
        while self.s[self.i] not in ",}]\r\n":
            self.i += 1
        token = self.s[start:self.i].strip()
        return {"true": True, "false": False, "null": None}.get(token, token)


def load_i18n() -> dict:
    text = I18N_JS.read_text(encoding="utf-8")
    marker = "const I18N = "
    start = text.index(marker) + len(marker)
    return _JsLiteralParser(text, start).value()


def key_shapes(node, prefix: str = "") -> dict[str, str]:
    """Map every leaf key path to a shape token ('str', 'array[3]', ...)."""
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(key_shapes(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(node, list):
        out[prefix] = f"array[{len(node)}]"
    else:
        out[prefix] = type(node).__name__
    return out


def leaf_strings(node, prefix: str = "") -> dict[str, str]:
    """Flatten to leaf strings, indexing into arrays so entries compare 1:1."""
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(leaf_strings(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(node, list):
        for idx, v in enumerate(node):
            out.update(leaf_strings(v, f"{prefix}[{idx}]"))
    else:
        out[prefix] = node if isinstance(node, str) else str(node)
    return out


def test_all_languages_present():
    data = load_i18n()
    for lang in LANGS:
        assert lang in data, f"I18N is missing the {lang!r} tree"
    print(f"OK: i18n languages present ({', '.join(LANGS)})")


def test_key_sets_identical_across_languages():
    data = load_i18n()
    shapes = {lang: key_shapes(data[lang]) for lang in LANGS}
    base = shapes[BASE_LANG]

    for lang in LANGS:
        if lang == BASE_LANG:
            continue
        missing = sorted(set(base) - set(shapes[lang]))
        extra = sorted(set(shapes[lang]) - set(base))
        assert not missing, (
            f"{lang} is missing {len(missing)} key(s) present in {BASE_LANG}: {missing[:10]}"
        )
        assert not extra, (
            f"{lang} has {len(extra)} key(s) not present in {BASE_LANG}: {extra[:10]}"
        )
    print(f"OK: i18n key parity ({len(base)} keys x {len(LANGS)} languages)")


def test_value_shapes_match():
    """Arrays are indexed by position (stepsMigrate etc.) — lengths must agree."""
    data = load_i18n()
    shapes = {lang: key_shapes(data[lang]) for lang in LANGS}
    base = shapes[BASE_LANG]

    for lang in LANGS:
        if lang == BASE_LANG:
            continue
        mismatched = {
            key: (base[key], shapes[lang][key])
            for key in base
            if key in shapes[lang] and base[key] != shapes[lang][key]
        }
        assert not mismatched, f"{lang} value-shape mismatches vs {BASE_LANG}: {mismatched}"
    print("OK: i18n value shapes match")


def test_placeholders_match():
    """A '{db}' dropped from one translation silently renders the raw token."""
    data = load_i18n()
    strings = {lang: leaf_strings(data[lang]) for lang in LANGS}
    base = strings[BASE_LANG]

    problems: list[str] = []
    for key, base_text in base.items():
        expected = set(PLACEHOLDER.findall(base_text))
        for lang in LANGS:
            if lang == BASE_LANG:
                continue
            found = set(PLACEHOLDER.findall(strings[lang].get(key, "")))
            if found != expected:
                problems.append(f"{key}: {BASE_LANG}={sorted(expected)} {lang}={sorted(found)}")
    assert not problems, "placeholder mismatches:\n  " + "\n  ".join(problems)
    print("OK: i18n placeholders consistent")


if __name__ == "__main__":
    test_all_languages_present()
    test_key_sets_identical_across_languages()
    test_value_shapes_match()
    test_placeholders_match()
    print("\nAll i18n parity tests passed.")
