"""CLI output contracts."""

import io

from corecoder.cli import _print_memory_text, _stream_token


def test_stream_token_does_not_crash_on_unencodable_unicode():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")

    _stream_token("done ✅", stream=stream)
    stream.flush()

    assert raw.getvalue().decode("gbk") == "done ?"


def test_memory_text_does_not_crash_on_legacy_windows_encoding():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")

    _print_memory_text("# 记忆\n\n- verified ✅", stream=stream)
    stream.flush()

    decoded = raw.getvalue().decode("gbk").replace("\r\n", "\n")
    assert decoded == "# 记忆\n\n- verified ?\n"
