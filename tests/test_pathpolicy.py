"""Tests for path policy: confinement, expansion-syntax, path rule matching."""

from redcell.pathpolicy import (
    has_expansion_syntax,
    path_rule_matches,
    within_root,
)


def test_has_expansion_syntax():
    assert has_expansion_syntax("$HOME/x")
    assert has_expansion_syntax("~root/.ssh")
    assert has_expansion_syntax("=rg")
    assert has_expansion_syntax("`whoami`")
    assert not has_expansion_syntax("/home/redcell/sandbox/file.txt")


def test_within_root_resolves_traversal(tmp_path):
    root = tmp_path / "sandbox"
    (root).mkdir()
    inside = root / "a" / "b.txt"
    assert within_root(str(inside), str(root))
    # ../ traversal out of the root is rejected after canonicalization.
    assert not within_root(str(root / ".." / "secret.txt"), str(root))
    assert not within_root("/etc/passwd", str(root))


def test_path_rule_matches_prefix_containment(tmp_path):
    root = str(tmp_path)
    # A rule on the dir matches reads of files under it.
    assert path_rule_matches(root, {"path": f"{root}/notes/a.txt"})
    assert not path_rule_matches(root, {"path": "/etc/shadow"})


def test_path_rule_matches_flags_expansion_syntax():
    # A path with expansion syntax always matches (so a deny rule catches it).
    assert path_rule_matches("/anything", {"path": "$HOME/.ssh/id_rsa"})


def test_path_rule_no_path_argument():
    assert path_rule_matches("/x", {"query": "no path here"}) is False
