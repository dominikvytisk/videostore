import os

from videostore.compression.engine import Algorithm, compress_file, decide_auto, decompress_file


def test_zstd_roundtrip(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"the quick brown fox jumps over the lazy dog. " * 5000)
    comp = tmp_path / "comp.bin"
    decomp = tmp_path / "decomp.bin"

    compress_file(str(src), str(comp), Algorithm.ZSTD)
    assert os.path.getsize(comp) < os.path.getsize(src)
    decompress_file(str(comp), str(decomp), Algorithm.ZSTD)
    assert decomp.read_bytes() == src.read_bytes()


def test_none_is_passthrough(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(1000))
    comp = tmp_path / "comp.bin"
    compress_file(str(src), str(comp), Algorithm.NONE)
    assert comp.read_bytes() == src.read_bytes()


def test_auto_skips_incompressible_data(tmp_path):
    src = tmp_path / "random.bin"
    src.write_bytes(os.urandom(1 << 20))
    assert decide_auto(str(src)) == Algorithm.NONE


def test_auto_picks_zstd_for_compressible_data(tmp_path):
    src = tmp_path / "text.bin"
    src.write_bytes(b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" * 20000)
    assert decide_auto(str(src)) == Algorithm.ZSTD
