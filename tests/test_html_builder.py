"""Tests for the markdown -> HTML builder (sanitization rules)."""
import re

from html_builder import (
    build_group_html,
    build_mod_html,
    remove_images,
    remove_images_and_links,
    _md,
    _sanitize_html,
)

_DANGEROUS_README = """# My mod

<div style="transform: rotate(-20deg); animation: spin 2s">badge</div>
<span style="position:absolute;top:-9999px">hidden</span>

<style>body { transform: none !important }</style>
After the style block.
"""


def test_markdownit_escapes_raw_html():
    rendered = _md.render(_DANGEROUS_README)
    # Raw HTML must be escaped to text, never injected as tags
    assert "<div" not in rendered
    assert "<style" not in rendered
    assert "&lt;div style=" in rendered


def test_no_style_tag_or_style_attr_survives_full_build():
    html = build_mod_html(
        "Owner/Mod",
        {"version": "1.0.0", "author": "Owner", "downloads": 10, "dependencies": []},
        _DANGEROUS_README,
        "",
    )
    assert "<style" not in html
    assert re.search(r"<[^>]*\sstyle=", html) is None


def test_generated_css_never_contains_transform_override():
    group_html = build_group_html("Test", [])
    assert "transform: none !important" not in group_html
    assert "!important" not in group_html


def test_sanitize_strips_real_tag_style_attrs_only():
    # Real tags: attribute removed, tag kept.
    assert _sanitize_html('<p>ok<span style="color:red">x</span></p>') == "<p>ok<span>x</span></p>"
    assert _sanitize_html("<div style='color:red'>x</div>") == "<div>x</div>"
    assert _sanitize_html("<div style=color:red>x</div>") == "<div>x</div>"
    # Escaped (printed-as-text) HTML must never be mangled by the scrubber.
    assert _sanitize_html("&lt;div style=&quot;color:red&quot;&gt;") == (
        "&lt;div style=&quot;color:red&quot;&gt;"
    )


def test_remove_images_keeps_links():
    text = "![alt](img.png) and [link](https://example.com) and ![x](y.gif)"
    assert remove_images(text) == " and [link](https://example.com) and "
    assert "![" not in remove_images(text)


def test_remove_images_and_links_flattens():
    text = "![alt](img.png) see [docs](https://example.com)"
    assert remove_images_and_links(text) == " see docs"


def test_build_mod_html_escapes_user_fields():
    html = build_mod_html(
        '<script>alert(1)</script>/Mod',
        {"version": '"><img src=x>', "author": "A&B", "downloads": 0, "dependencies": []},
        "readme",
        "",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert '"><img' not in html
    assert "A&amp;B" in html


def test_images_never_reach_the_html():
    html = build_mod_html(
        "Owner/Mod",
        {"version": "1.0", "author": "Owner", "downloads": 1, "dependencies": []},
        "Intro text\n\n![screenshot](https://example.com/s.png)\n\nmore text",
        "",
    )
    assert "<img" not in html
    assert "screenshot" not in html


def test_toc_anchors_are_unique():
    mods = [
        {"name": "A/B", "metadata": {}, "readme": "r", "changelog": ""},
        {"name": "A_B", "metadata": {}, "readme": "r", "changelog": ""},
        {"name": "A/B", "metadata": {}, "readme": "r", "changelog": ""},
    ]
    html = build_group_html("Group", mods)
    # ids used in the document
    ids = re.findall(r'<div class="mod-section" id="([^"]+)"', html)
    assert len(ids) == len(set(ids)) == 3
    # every toc link points at an existing id
    for m in re.finditer(r'<a href="#([^"]+)"', html):
        assert m.group(1) in ids


def test_group_title_and_name_escaped():
    html = build_group_html('A "weird" <name>', [])
    assert 'A &quot;weird&quot; &lt;name&gt;' in html
    assert 'A "weird" <name>' not in html
