from pathlib import Path

from prosecution_data.config import load_config


def test_load_config_resolves_paths_and_reads_secret_only_from_environment(tmp_path: Path) -> None:
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        """
output_root = "artifacts"
sample_size = 25
random_seed = 17
api_key = "must-not-be-used"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path, {"USPTO_API_KEY": "top-secret"})

    assert config.output_root == tmp_path / "artifacts"
    assert config.sample_size == 25
    assert config.random_seed == 17
    assert config.api_key == "top-secret"
    assert "top-secret" not in repr(config)
    assert "must-not-be-used" not in repr(config)


def test_load_config_is_usable_without_api_key(tmp_path: Path) -> None:
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text('output_root = "data"', encoding="utf-8")

    config = load_config(config_path, {})

    assert config.api_key is None
    assert config.output_root == tmp_path / "data"
