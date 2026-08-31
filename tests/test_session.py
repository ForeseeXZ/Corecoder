import pytest

from corecoder import session as session_module
from corecoder.session import load_session, save_session


def test_default_session_ids_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    first_id = save_session([{"role": "user", "content": "first"}], "model-a")
    second_id = save_session([{"role": "user", "content": "second"}], "model-b")

    assert first_id != second_id
    assert load_session(first_id) == (
        [{"role": "user", "content": "first"}],
        "model-a",
    )
    assert load_session(second_id) == (
        [{"role": "user", "content": "second"}],
        "model-b",
    )


def test_corrupt_session_is_reported_as_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    (tmp_path / "broken.json").write_text("{not valid json", encoding="utf-8")

    assert load_session("broken") is None


def test_session_round_trips_utf8_content(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    messages = [{"role": "user", "content": "读取 中文目录/说明.md"}]

    session_id = save_session(messages, "测试模型", "中文会话")

    assert load_session(session_id) == (messages, "测试模型")


def test_long_session_id_stays_within_a_portable_filename_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    messages = [{"role": "user", "content": "long name"}]

    session_id = save_session(messages, "model", "x" * 400)

    assert len(session_id) <= 100
    assert load_session(session_id) == (messages, "model")


@pytest.mark.parametrize(
    "requested_id",
    ["../outside", "/absolute/outside", r"C:\outside\windows-name"],
)
def test_session_id_cannot_escape_the_session_directory(
    tmp_path, monkeypatch, requested_id
):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    session_id = save_session([], "model", requested_id)

    assert (tmp_path / f"{session_id}.json").is_file()
    assert load_session(requested_id) == ([], "model")
