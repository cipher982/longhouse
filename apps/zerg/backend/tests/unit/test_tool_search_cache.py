import numpy as np


def test_tool_search_cache_disallows_pickle(tmp_path, monkeypatch):
    from zerg.tools import tool_search

    cache_file = tmp_path / "tool_embeddings.npz"
    cache_file.write_bytes(b"stub")
    monkeypatch.setattr(tool_search, "EMBEDDING_CACHE_FILE", cache_file)

    catalog_hash = "hash"
    expected_names = ["tool-a"]
    embeddings = np.zeros((1, 3))

    class DummyArchive:
        def get(self, key, default=None):
            if key == "catalog_hash":
                return catalog_hash
            if key == "tool_names":
                return expected_names
            return default

        def __getitem__(self, key):
            if key == "embeddings":
                return embeddings
            raise KeyError(key)

    def fake_load(path, allow_pickle=False):
        assert allow_pickle is False
        return DummyArchive()

    monkeypatch.setattr(tool_search.np, "load", fake_load)

    loaded = tool_search._load_embeddings_cache(catalog_hash, expected_names)
    assert loaded is embeddings
