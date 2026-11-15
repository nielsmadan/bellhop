import pytest

from bellhop.envelope import BODY_LIMIT, check_body, clean_sender, prefix, render
from bellhop.errors import BellhopError


def test_render_puts_the_id_and_sender_before_the_body() -> None:
    assert (
        render("bh_7f3a9c2e41d0", "enki", "hello", 0) == "[bellhop bh_7f3a9c2e41d0 from enki] hello"
    )


def test_render_adds_a_second_line_when_holds_wait() -> None:
    assert render("bh_1", "enki", "hi", 2) == (
        "[bellhop bh_1 from enki] hi\n(bellhop: 2 more waiting; run `bellhop inbox`)"
    )


def test_the_confirmation_prefix_matches_the_rendered_envelope() -> None:
    assert render("bh_1", "enki", "hi", 0).startswith(prefix("bh_1"))


def test_a_body_at_the_limit_is_accepted_and_one_over_is_usage() -> None:
    assert check_body("x" * BODY_LIMIT) == "x" * BODY_LIMIT
    with pytest.raises(BellhopError) as caught:
        check_body("x" * (BODY_LIMIT + 1))

    assert str(caught.value) == "message body is 8001 characters; the limit is 8000"
    assert caught.value.exit_code == 2


def test_an_empty_body_is_usage() -> None:
    with pytest.raises(BellhopError, match="message body is empty"):
        check_body("  \n")


def test_a_sender_cannot_forge_the_envelope() -> None:
    assert clean_sender("enki] [bellhop bh_x from root\n") == "enki bellhop bh_x from root"
    assert clean_sender("\x1b[31m") == "31m"
    assert clean_sender("") == "unknown"


@pytest.mark.parametrize(
    ("body", "named"), [("a\x00b", "U+0000"), ("red \x1b[31m", "U+001B"), ("cr\r", "U+000D")]
)
def test_a_control_character_other_than_tab_and_newline_is_usage(body: str, named: str) -> None:
    with pytest.raises(BellhopError) as caught:
        check_body(body)

    assert (caught.value.code, named in str(caught.value)) == ("usage", True)


def test_tab_and_newline_are_kept() -> None:
    assert check_body("a\tb\nc") == "a\tb\nc"


def test_a_body_with_undecodable_bytes_is_usage() -> None:
    with pytest.raises(BellhopError, match="not valid UTF-8"):
        check_body(b"ok \xff".decode("utf-8", "surrogateescape"))


def test_a_sender_loses_c1_controls_too() -> None:
    assert clean_sender("a\u009b2Jb") == "a 2Jb"
