import argparse

from zerg.qa.provider_factory_invocation import add_factory_provider_arguments


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_factory_provider_arguments(
        parser,
        variants=("cell:claude:test:scenario",),
        provider_bin_aliases=("--claude-bin",),
    )
    return parser


def test_factory_provider_envelope_uses_canonical_binary_name():
    args = _parser().parse_args(
        [
            "--variant",
            "cell:claude:test:scenario",
            "--evidence-root",
            "/evidence",
            "--repo-root",
            "/repo",
            "--engine",
            "/bin/engine",
            "--longhouse-cli",
            "/bin/longhouse",
            "--provider-bin",
            "/bin/provider",
            "--provider-version",
            "1.2.3",
        ]
    )

    assert str(args.provider_bin) == "/bin/provider"
    assert str(args.longhouse_cli) == "/bin/longhouse"
    assert args.provider_version == "1.2.3"


def test_historical_binary_name_is_only_a_public_cli_alias():
    args = _parser().parse_args(
        [
            "--variant",
            "cell:claude:test:scenario",
            "--claude-bin",
            "/bin/provider",
        ]
    )

    assert str(args.provider_bin) == "/bin/provider"
