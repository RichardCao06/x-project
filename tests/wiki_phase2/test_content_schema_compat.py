from __future__ import annotations

from run_wiki_content_capture import sanitize_output_schema


def test_content_runtime_schema_removes_only_api_unsupported_uniqueness_keyword() -> None:
    source = {"type": "object", "properties": {"ids": {
        "type": "array", "uniqueItems": True, "items": {"type": "string"}
    }}, "required": ["ids"], "additionalProperties": False}
    result = sanitize_output_schema(source)
    assert "uniqueItems" not in result["properties"]["ids"]
    assert result["required"] == ["ids"] and result["additionalProperties"] is False
    assert source["properties"]["ids"]["uniqueItems"] is True
