from __future__ import annotations

from akashic.offline import apply_offline_env


def test_apply_offline_env_sets_the_flags_and_keeps_other_variables() -> None:
    env: dict[str, str] = {"HF_HUB_OFFLINE": "0", "PATH": "x"}
    apply_offline_env(env)
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["HF_DATASETS_OFFLINE"] == "1"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert env["PATH"] == "x"
