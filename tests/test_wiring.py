from pathlib import Path

import pytest

from bellhop.errors import BellhopError
from bellhop.routes.pi import PiRoute
from bellhop.wiring import extension_transcript, pi_agent_dir
from conftest import make_session


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, Path("/home/me/.pi/agent")),
        ("~", Path("/home/me")),
        ("~/pi-agent", Path("/home/me/pi-agent")),
        ("/opt/pi-agent", Path("/opt/pi-agent")),
        ("relative/agent", None),
    ],
)
def test_pi_agent_dir_follows_pi_and_refuses_a_relative_value(
    value: str | None, expected: Path | None
) -> None:
    env = {"HOME": "/home/me"} | ({"PI_CODING_AGENT_DIR": value} if value else {})

    assert pi_agent_dir(env) == expected


def test_a_runtime_dir_problem_means_no_reported_transcript(tmp_path: Path) -> None:
    def unsafe() -> Path:
        raise BellhopError("runtime directory is unsafe", code="runtime_dir_unsafe")

    reported = extension_transcript(PiRoute(unsafe))

    assert reported(make_session("p1", harness="pi", pid=4242)) is None
