import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load_demo():
    spec = importlib.util.spec_from_file_location("havoc365_demo", ROOT / "assets" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recording_runs_real_map_offline_and_labels_omitted_dns(monkeypatch, tmp_path, capsys):
    demo = load_demo()
    monkeypatch.chdir(tmp_path)
    assert demo.main(["--no-interactive"]) == 0
    output = capsys.readouterr().out
    assert "OFFLINE DEMO / havoc365 map" in output
    assert "Domains omitted: no DNS or tenant calls." in output
    assert "1 runbook search matches" in output
    assert "1 enabled enterprise apps" in output
    assert "SKIPPED" in output
    assert "has no MX records" not in output
    assert (tmp_path / "blast-radius.md").exists()


def test_demo_cannot_start_a_drill_or_fall_back_to_network():
    demo = load_demo()
    with pytest.raises(SystemExit, match="only supports map"):
        demo.main(["drill", "run"])
    with pytest.raises(AssertionError, match="read-only"):
        demo.ExampleAuth(ROOT).token(True)


def test_recorded_interaction_is_embedded_after_description():
    readme = (ROOT / "README.md").read_text()
    assert readme.index("havoc365 maps these dependencies") < readme.index('src="assets/map.gif"')
    assert readme.index('src="assets/map.gif"') < readme.index("## Getting Started")
    assert 'srcset="assets/map.png"' in readme
