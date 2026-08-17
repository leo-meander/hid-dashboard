"""No backslash inside an f-string expression — the container runs Python 3.11.

This exists because a syntax error shipped to production and crash-looped the
service. The line parsed fine locally (Python 3.12 lifted the restriction in
PEP 701) and fine in CI, and only failed on the `python:3.11-slim` image at
import time — the one place nothing was checking.

`ast.parse(..., feature_version=(3, 11))` does NOT catch it. The restriction
lives in the 3.11 tokenizer, and CPython always tokenizes with the running
interpreter's tokenizer regardless of `feature_version` — so that call returns
clean on exactly the code that breaks. `test_the_checker_catches_the_line_that_
broke_production` is the guard against this test quietly becoming decorative.

If the deployment image ever moves to 3.12+, delete this file rather than
letting it rot — the rule it enforces stops being real at that point.
"""
import io
import pathlib
import tokenize

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent


def backslash_in_fstring_expression(source: str) -> list[tuple[int, str]]:
    """Every `(lineno, snippet)` a Python 3.11 tokenizer would reject.

    Python 3.12 tokenizes an f-string into FSTRING_START / FSTRING_MIDDLE /
    FSTRING_END, with the replacement fields emitted as ordinary tokens in
    between. The literal text of the f-string is the MIDDLE tokens — where a
    backslash is fine on every version — so anything else appearing between
    START and END is expression text, where 3.11 forbids one.
    """
    found: list[tuple[int, str]] = []
    depth = 0
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        if tok.type == tokenize.FSTRING_START:
            depth += 1
        elif tok.type == tokenize.FSTRING_END:
            depth = max(0, depth - 1)
        elif depth > 0 and tok.type != tokenize.FSTRING_MIDDLE:
            if "\\" in tok.string:
                found.append((tok.start[0], tok.string.strip()))
    return found


def _python_files() -> list[pathlib.Path]:
    return [
        p
        for d in ("app", "tests", "alembic")
        for p in sorted((BACKEND / d).rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


class TestTheCheckerItself:
    """A checker that cannot fail is worse than no checker: it reports clean
    and everyone believes it."""

    def test_catches_the_line_that_broke_production(self):
        broken = (
            '''x = f"{'<span style=\\"color:' + C['muted'] '''
            ''' + ';\\"> ok</span>' if u else ''}"'''
        )
        assert backslash_in_fstring_expression(broken)

    @pytest.mark.parametrize("src", [
        # A backslash in the LITERAL part is legal on every version.
        r'x = f"a\nb {value}"',
        # Nested quotes with no backslash are what the fix uses instead.
        """x = f"<span style='color:{c}'>hi</span>" """,
        # A plain (non-f) string may contain whatever it likes, including
        # something that looks like a replacement field.
        r'x = "{\"json\": 1}\n"',
        # Format specs and conversions are not expressions with backslashes.
        'x = f"{value:.2f} {other!r}"',
        # Nested f-strings, still clean.
        'x = f"{f\'{inner}\'}"',
    ])
    def test_does_not_flag_legal_code(self, src):
        assert backslash_in_fstring_expression(src) == []


class TestBackendSource:
    def test_no_file_uses_a_backslash_in_an_fstring_expression(self):
        offenders = []
        for path in _python_files():
            for lineno, snippet in backslash_in_fstring_expression(
                path.read_text(encoding="utf-8")
            ):
                offenders.append(
                    f"{path.relative_to(BACKEND)}:{lineno}: {snippet}"
                )
        assert not offenders, (
            "Python 3.11 rejects a backslash inside an f-string expression, and "
            "the deployment image is python:3.11-slim — these would import-crash "
            "in production while parsing fine locally on 3.12:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_scan_actually_reaches_the_source(self):
        """Guards the glob itself — a scan over zero files also passes."""
        files = _python_files()
        assert len(files) > 100
        assert any(p.name == "biweekly_share.py" for p in files)
