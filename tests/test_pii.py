"""The PII detector finds personal data in text, the working tree and git history."""
import subprocess
from pathlib import Path

import pytest

from big_brother.pii import load_terms, main, mask, scan_history, scan_text, scan_tree


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout


def kinds(findings) -> list[str]:
    return [f.kind for f in findings]


# text

def test_finds_personal_email():
    found = scan_text("contact jane.doe42@gmail.com today")  # pii: fake
    assert kinds(found) == ["email"]
    assert found[0].match == "jane.doe42@gmail.com"  # pii: fake
    assert found[0].line == 1


@pytest.mark.parametrize("safe", [
    "t@example.com",
    "noreply@anthropic.com",
    "123+someone@users.noreply.github.com",
    "a@test.example.org",
])
def test_allowlisted_emails_pass(safe):
    assert scan_text(f"x {safe} y") == []


def test_escaped_newline_before_decorator_is_not_an_email():
    assert scan_text(r'"import pytest\n@pytest.fixture\ndef f(): ..."') == []


def test_email_after_escape_sequence_is_still_found():
    found = scan_text(r'"name:\tjane@gmail.com"')  # pii: fake
    assert [f.match for f in found] == ["jane@gmail.com"]  # pii: fake


@pytest.mark.parametrize("phone", ["(989) 555-0142", "989-555-0142", "989.555.0142",  # pii: fake
                                   "+1 989 555 0142"])  # pii: fake
def test_finds_phone_numbers(phone):
    assert kinds(scan_text(f"call {phone}")) == ["phone"]


@pytest.mark.parametrize("not_phone", ["commit 9ad3466", "1.9 GB", "2026-09-28",
                                       "version 0.12.11", "id 68811988"])
def test_ignores_numbers_that_are_not_phones(not_phone):
    assert scan_text(not_phone) == []


def test_finds_home_paths_but_not_tilde_or_generic_user():
    found = scan_text("see /home/alice/notes and ~/notes and /home/user/x and /Users/bob/y")  # pii: fake
    assert [(f.kind, f.match) for f in found] == [("home_path", "/home/alice"),  # pii: fake
                                                 ("home_path", "/Users/bob")]  # pii: fake


def test_finds_denylisted_terms_as_whole_words_any_case():
    found = scan_text("Alice ran on laptop-host.\nalicex is fine\nALICE again", terms=["alice", "laptop-host"])
    assert [(f.line, f.match) for f in found] == [(1, "Alice"), (1, "laptop-host"), (3, "ALICE")]


def test_term_inside_a_longer_word_is_not_flagged():
    assert scan_text("MaryJane", terms=["jane"]) == []


def test_lines_marked_fake_are_skipped():
    assert scan_text('x = "jane@gmail.com"  # pii: fake\nalice', terms=["alice"]) == [  # pii: fake
        scan_text("\nalice", terms=["alice"])[0]]


def test_mask_hides_most_of_the_value():
    assert mask("jane.doe42@gmail.com") == "j***@gmail.com"  # pii: fake
    assert mask("alice") == "a***"


# terms file

def test_load_terms_skips_blanks_and_comments(tmp_path):
    f = tmp_path / "terms.txt"
    f.write_text("# names\nalice\n\n  laptop-host  \n")
    assert load_terms(f) == ["alice", "laptop-host"]


def test_load_terms_missing_file_is_empty(tmp_path):
    assert load_terms(tmp_path / "nope.txt") == []


# repositories

@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("clean\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "clean")
    return tmp_path


def test_scan_tree_reports_path_and_line_and_skips_ignored(repo):
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("jane@gmail.com\n")  # pii: fake
    (repo / "b.md").write_text("ok\nhost alice here\n")
    found = scan_tree(repo, terms=["alice"])
    assert [(f.path, f.line, f.kind) for f in found] == [("b.md", 2, "term")]


def test_scan_tree_skips_binary_files(repo):
    (repo / "blob.bin").write_bytes(b"\0\xffjane@gmail.com")  # pii: fake
    assert scan_tree(repo) == []


def test_scan_history_finds_commit_identity(repo):
    env = {"GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "alice@gmail.com",  # pii: fake
           "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "alice@gmail.com",  # pii: fake
           "PATH": "/usr/bin:/bin", "HOME": str(repo)}
    (repo / "a.txt").write_text("changed\n")
    git(repo, "commit", "-qam", "by alice", env=env)
    found = scan_history(repo, terms=["alice"])
    assert {f.kind for f in found} == {"email", "term"}
    assert all(f.path.startswith("commit ") for f in found)


def test_scan_history_finds_pii_removed_from_the_tree(repo):
    (repo / "a.txt").write_text("jane@gmail.com\n")  # pii: fake
    git(repo, "commit", "-qam", "leak")
    (repo / "a.txt").write_text("clean\n")
    git(repo, "commit", "-qam", "unleak")
    assert scan_tree(repo) == []
    assert kinds(scan_history(repo)) == ["email"]


# command line

def test_main_exit_codes_and_masked_output(repo, tmp_path_factory, capsys):
    terms = tmp_path_factory.mktemp("cfg") / "terms.txt"
    terms.write_text("alice\n")
    assert main([str(repo), "--terms", str(terms)]) == 0
    (repo / "b.md").write_text("mail jane@gmail.com\n")  # pii: fake
    assert main([str(repo), "--terms", str(terms)]) == 1
    out = capsys.readouterr().out
    assert "b.md:1: email j***@gmail.com" in out  # pii: fake
    assert "jane@gmail.com" not in out  # pii: fake


def test_main_history_flag(repo, tmp_path_factory):
    (repo / "a.txt").write_text("jane@gmail.com\n")  # pii: fake
    git(repo, "commit", "-qam", "leak")
    (repo / "a.txt").write_text("clean\n")
    git(repo, "commit", "-qam", "unleak")
    empty = tmp_path_factory.mktemp("cfg") / "none.txt"
    assert main([str(repo), "--terms", str(empty)]) == 0
    assert main([str(repo), "--terms", str(empty), "--history"]) == 1
