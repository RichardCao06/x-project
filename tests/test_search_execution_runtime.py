from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

from lca_project.kernel.search_cache import fetch_cache_key, query_cache_key
from lca_project.kernel.search_execution import SearchExecutionRuntime
from lca_project.kernel.state import StateStore


def runtime(tmp_path: Path) -> SearchExecutionRuntime:
    return SearchExecutionRuntime(tmp_path / "cache", StateStore(tmp_path / "state.db"),
                                  global_limit=4, provider_limit=2)


def test_cache_keys_split_search_consumption_from_fetch_policy() -> None:
    one = query_cache_key(query="  SMT  Energy ", language="en", provider_id="exa",
                          provider_config_version="1", routing_policy_version="1")
    two = query_cache_key(query="smt energy", language="en", provider_id="exa",
                          provider_config_version="1", routing_policy_version="1")
    assert one == two
    assert one != query_cache_key(query="smt energy", language="en", provider_id="exa",
                                  provider_config_version="2", routing_policy_version="1")
    assert fetch_cache_key(url="HTTPS://Example.com/a#x", fetch_policy_version="1",
                           accepted_media_types=["text/html"], extractor_version="1") == \
           fetch_cache_key(url="https://example.com/a", fetch_policy_version="1",
                           accepted_media_types=["text/html"], extractor_version="1")


def test_query_cache_and_checkpoint_prevent_duplicate_external_calls(tmp_path: Path) -> None:
    rt = runtime(tmp_path); calls = 0
    def execute(row):
        nonlocal calls
        value, hit, key = rt.search(query=row["query"], language="en", provider_id="exa",
            provider_config_version="1", routing_policy_version="1",
            operation=lambda: {"hits": [row["query"]]})
        calls += int(not hit)
        return {"query_id": row["query_id"], "cache_key": key, **value}
    rows = [{"query_id": f"q{i}", "query": f"query {i}"} for i in range(5)]
    first = rt.execute(rows, execution_dir=tmp_path / "execution", execute_one=execute)
    second = rt.execute(rows, execution_dir=tmp_path / "execution", execute_one=execute)
    assert calls == 5 and first.query_cache_misses == 5
    assert second.checkpoint_hits == 5
    assert second.manifest_path.is_file()


def test_incremental_execution_runs_only_new_query(tmp_path: Path) -> None:
    rt = runtime(tmp_path); calls: list[str] = []
    def execute(row): calls.append(row["query_id"]); return {"id": row["query_id"]}
    rows = [{"query_id": "q1", "query": "one"}, {"query_id": "q2", "query": "two"}]
    rt.execute(rows, execution_dir=tmp_path / "execution", execute_one=execute)
    rt.execute([*rows, {"query_id": "q3", "query": "three"}],
               execution_dir=tmp_path / "execution", execute_one=execute)
    assert calls == ["q1", "q2", "q3"]


def test_global_provider_limit_applies_across_runtime_instances(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    runtimes = [SearchExecutionRuntime(tmp_path / "cache", state, global_limit=6,
                                       provider_limit=2) for _ in range(6)]
    lock = threading.Lock(); active = peak = 0
    def call(index):
        nonlocal active, peak
        def operation():
            nonlocal active, peak
            with lock: active += 1; peak = max(peak, active)
            time.sleep(0.05)
            with lock: active -= 1
            return {"hits": [index]}
        return runtimes[index].search(query=f"q{index}", language="en", provider_id="exa",
            provider_config_version="1", routing_policy_version="1", operation=operation)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(call, range(6)))
    assert peak <= 2
