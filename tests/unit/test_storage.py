"""Unit: the fsspec storage abstraction over the local filesystem (DESIGN §4, #14).

Local paths exercise the same read/write/exists API the cluster uses for ``gs://`` (the driver swaps
by URI scheme); the gs:// path is covered by the in-cluster verification.
"""
from eval_engine import storage


def test_write_read_bytes_roundtrip(tmp_path):
    uri = str(tmp_path / "a.bin")
    storage.write_bytes(uri, b"\x00\x01hello")
    assert storage.read_bytes(uri) == b"\x00\x01hello"


def test_write_read_text_roundtrip(tmp_path):
    uri = str(tmp_path / "a.txt")
    storage.write_text(uri, "héllo")          # non-ASCII survives the utf-8 round trip
    assert storage.read_text(uri) == "héllo"


def test_write_creates_missing_parent_dirs(tmp_path):
    uri = str(tmp_path / "deep" / "nested" / "dir" / "f.json")
    storage.write_text(uri, "{}")             # parents don't exist yet
    assert storage.read_text(uri) == "{}"


def test_exists(tmp_path):
    uri = str(tmp_path / "present.txt")
    assert storage.exists(uri) is False
    storage.write_text(uri, "x")
    assert storage.exists(uri) is True
