from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("search_provider_runtime", ROOT / "scripts/search_provider_runtime.py")
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def test_normalize_hits_deduplicates_and_preserves_provider_locator() -> None:
    rows = [{"url": "https://example.com/a", "title": "A", "description": "one"},
            {"url": "https://example.com/a", "title": "duplicate"},
            {"url": "javascript:bad", "title": "bad"}]
    hits = module().normalize_hits(rows, provider="exa", locator="solder dross", limit=10)
    assert hits == [{"url": "https://example.com/a", "title": "A", "snippet": "one",
                     "provider": "exa", "locator": "solder dross"}]


def test_secret_loader_does_not_override_process_environment(tmp_path: Path, monkeypatch) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text("EXA_API_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setenv("EXA_API_KEY", "environment-value")
    assert module().load_secrets(secret_file)["EXA_API_KEY"] == "environment-value"
