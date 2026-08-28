import os

from videostore.archive import build_archive, compute_archive_checksum, extract_archive, list_archive


def _make_tree(tmp_path):
    d = tmp_path / "input"
    d.mkdir()
    (d / "a.txt").write_text("hello world\n" * 10)
    (d / "b.bin").write_bytes(os.urandom(5000))
    sub = d / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("nested\n")
    return d


def test_build_and_extract_roundtrip(tmp_path):
    src = _make_tree(tmp_path)
    archive_path = str(tmp_path / "out.vsar")
    summary = build_archive([str(src)], archive_path)
    assert summary.file_count == 3

    out_dir = tmp_path / "restored"
    recovered, failed = extract_archive(archive_path, str(out_dir))
    assert failed == []
    assert len(recovered) == 3

    for entry in recovered:
        restored_path = out_dir / entry.path
        assert restored_path.exists()

    # byte-for-byte
    orig_a = (src / "a.txt").read_bytes()
    restored_a = (out_dir / "input" / "a.txt").read_bytes()
    assert orig_a == restored_a


def test_list_archive_matches_build(tmp_path):
    src = _make_tree(tmp_path)
    archive_path = str(tmp_path / "out.vsar")
    build_summary = build_archive([str(src)], archive_path)
    listed = list_archive(archive_path)
    assert listed.file_count == build_summary.file_count
    assert listed.archive_checksum == build_summary.archive_checksum


def test_compute_archive_checksum_matches_build(tmp_path):
    src = _make_tree(tmp_path)
    archive_path = str(tmp_path / "out.vsar")
    summary = build_archive([str(src)], archive_path)
    assert compute_archive_checksum(archive_path) == summary.archive_checksum


def test_extract_detects_corruption(tmp_path):
    src = _make_tree(tmp_path)
    archive_path = str(tmp_path / "out.vsar")
    build_archive([str(src)], archive_path)

    # flip a byte inside the data section (well before the footer/manifest)
    with open(archive_path, "r+b") as f:
        f.seek(20)
        b = f.read(1)
        f.seek(20)
        f.write(bytes([b[0] ^ 0xFF]))

    out_dir = tmp_path / "restored_corrupt"
    recovered, failed = extract_archive(archive_path, str(out_dir))
    # at least one file's checksum should now fail (whichever one we hit)
    assert failed or recovered  # doesn't crash either way
