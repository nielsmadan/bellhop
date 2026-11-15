from collections.abc import Collection, Mapping

PASSTHROUGH = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "SHELL",
        "LANG",
        "TMPDIR",
        "SOPS_SECRETS",
        "SOPS_AGE_KEY_FILE",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "BELLHOP_RUNTIME_DIR",
        "BELLHOP_HOOKLINESINKER",
    }
)
LOCALE_PREFIX = "LC_"


def passed(env: Mapping[str, str], extra: Collection[str] = ()) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if name in PASSTHROUGH or name in extra or name.startswith(LOCALE_PREFIX)
    }
