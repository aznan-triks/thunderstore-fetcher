"""Tests for the Markdown / TXT text exports."""
import text_builder as tb


def _mod(readme="", changelog="", deps=None, name="Owner/Mod", downloads=42):
    return {
        "name": name,
        "metadata": {
            "version": "1.2.3",
            "author": "Owner",
            "downloads": downloads,
            "dependencies": deps or [],
            "category": "Misc",
        },
        "readme": readme,
        "changelog": changelog,
    }


def test_markdown_keeps_links_but_drops_images():
    readme = "Before\n\n![logo](https://x/logo.png)\n\nSee [the docs](https://docs.example.com) for **details**."
    md = tb.build_group_markdown("Misc", [_mod(readme=readme)])
    assert "[the docs](https://docs.example.com)" in md
    assert "![" not in md
    assert "## README" in md
    assert "**details**" in md  # markdown markup preserved


def test_markdown_includes_toc_and_changelog():
    md = tb.build_group_markdown(
        "Misc",
        [_mod(name="A/B", changelog="## 1.0\n- fixed stuff")],
    )
    assert "# Misc" in md
    assert "## Table of contents" in md
    assert "- A/B" in md
    assert "## Changelog" in md
    assert "- fixed stuff" in md


def test_txt_flattens_markdown():
    readme = (
        "# Heading\n\n> quote line\n\n- bullet one\n- bullet two\n\n"
        "**bold** and `code` and [link](https://example.com)\n\n"
        "![img](x.png)\n\nTrailing."
    )
    txt = tb.build_group_txt("Misc", [_mod(readme=readme)])
    assert "# Heading" not in txt
    assert "> quote" not in txt
    assert "•  bullet one" in txt or "• bullet one" in txt
    assert "**bold**" not in txt
    assert "[link](https://example.com)" not in txt
    assert "![" not in txt
    assert "Trailing." in txt
    assert "README" in txt


def test_word_counts():
    assert tb.count_words("") == 0
    assert tb.count_words("  a  b\tc ") == 3
    mod = _mod(readme="one two three", changelog="four five", deps=["dep-a", "dep-b"])
    # readme (3) + changelog (2) + version (1) + author (1) + deps (2)
    assert tb.mod_word_count(mod) == 9
