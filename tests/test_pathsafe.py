import pytest

from videostore.utils.pathsafe import UnsafePathError, safe_extract_path, sanitize_archive_path


def test_sanitize_normal_path():
    assert sanitize_archive_path("sub/dir/file.txt") == "sub/dir/file.txt"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "sub/../../escape.txt",
        "C:\\windows\\system32",
        "",
        "a\x00b",
    ],
)
def test_rejects_unsafe_paths(bad):
    with pytest.raises(UnsafePathError):
        sanitize_archive_path(bad)


def test_safe_extract_path_stays_inside_root(tmp_path):
    dest = safe_extract_path(str(tmp_path), "sub/file.txt")
    assert dest.startswith(str(tmp_path))


def test_safe_extract_path_rejects_traversal(tmp_path):
    with pytest.raises(UnsafePathError):
        safe_extract_path(str(tmp_path), "../../../etc/passwd")
