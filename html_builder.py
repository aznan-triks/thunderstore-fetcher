import html
import re
from markdown_it import MarkdownIt

# html=False: raw HTML in the readmes is escaped instead of being passed through as-is
# This prevents things like <div style="transform:..."> from reaching WeasyPrint
_md = MarkdownIt(options_update={"html": False})

_STRIP_STYLE_TAGS = re.compile(r'<style[^>]*>.*?</style>', re.DOTALL | re.IGNORECASE)
_STRIP_STYLE_ATTR = re.compile(r"""\s+style\s*=\s*(?:"[^"]*"|'[^']*')""", re.IGNORECASE)


def _sanitize_html(h: str) -> str:
    """Removes leftover style blocks and style= attributes."""
    h = _STRIP_STYLE_TAGS.sub('', h)
    h = _STRIP_STYLE_ATTR.sub('', h)
    return h


def remove_images_and_links(md_text: str) -> str:
    md_text = re.sub(r'!\[.*?\]\(.*?\)', '', md_text)
    md_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', md_text)
    return md_text


def _anchor(name: str) -> str:
    return re.sub(r'[^\w-]', '_', name)


def build_mod_html(name: str, metadata: dict, readme: str, changelog: str,
                   anchor_id: str | None = None) -> str:
    clean_readme    = remove_images_and_links(readme or "")
    clean_changelog = remove_images_and_links(changelog or "")

    readme_html    = _sanitize_html(_md.render(clean_readme))    if clean_readme.strip()    else "<p><em>No README available.</em></p>"
    changelog_html = _sanitize_html(_md.render(clean_changelog)) if clean_changelog.strip() else ""

    deps_raw  = metadata.get("dependencies", [])
    deps      = ", ".join(deps_raw) if isinstance(deps_raw, list) else str(deps_raw or "")
    deps      = deps or "None"
    downloads = "{:,}".format(metadata.get("downloads", 0))
    ename     = html.escape(name)
    aid       = anchor_id or _anchor(name)
    ch_block  = "<h2>Changelog</h2>" + changelog_html if changelog_html else ""

    return (
        '<div class="mod-section" id="' + aid + '">'
        + "<h1>" + ename + "</h1>"
        + '<table class="meta-table">'
        + "<tr><td><strong>Version</strong></td><td>"      + html.escape(str(metadata.get("version", "N/A")))    + "</td></tr>"
        + "<tr><td><strong>Author</strong></td><td>"       + html.escape(str(metadata.get("author", "Unknown"))) + "</td></tr>"
        + "<tr><td><strong>Downloads</strong></td><td>"    + downloads                                           + "</td></tr>"
        + "<tr><td><strong>Dependencies</strong></td><td>" + html.escape(deps)                                   + "</td></tr>"
        + "</table>"
        + "<h2>README</h2>" + readme_html
        + ch_block
        + "</div><hr>"
    )


_PAGE_CSS = (
    "body{font-family:sans-serif;margin:1cm;line-height:1.5}"
    "nav.toc{page-break-after:always}"
    "nav.toc ol{columns:2;column-gap:2em}"
    ".mod-section{page-break-before:always}"
    ".mod-section:first-of-type{page-break-before:avoid}"
    "hr{visibility:hidden}"
    ".meta-table{border-collapse:collapse;margin-bottom:1em;width:100%}"
    ".meta-table td{padding:.2em .6em;border:1px solid #ddd;vertical-align:top}"
    ".meta-table td:first-child{width:120px;background:#f5f5f5;font-weight:bold}"
    "a{color:#0066cc}"
    "pre{background:#f5f5f5;padding:.5em;white-space:pre-wrap;word-break:break-all}"
    "code{background:#f5f5f5;padding:.1em .3em;border-radius:3px}"
    "img{display:none}"
)


def build_group_html(group_name: str, mods_data: list) -> str:
    body      = ""
    toc_items = ""
    seen: dict[str, int] = {}   # anchor -> count, to guarantee uniqueness
    for mod in mods_data:
        n = mod["name"]
        base = _anchor(n)
        # Two different names can produce the same anchor (e.g. "A/B" and "A-B").
        # We suffix -2, -3… to avoid the table of contents pointing to the wrong spot.
        seen[base] = seen.get(base, 0) + 1
        a = base if seen[base] == 1 else f"{base}-{seen[base]}"
        toc_items += '<li><a href="#' + a + '">' + html.escape(n) + "</a></li>\n"
        body      += build_mod_html(n, mod["metadata"], mod["readme"], mod["changelog"], anchor_id=a)

    eg = html.escape(group_name or "Unnamed")
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        + '<meta charset="utf-8">\n'
        + "<title>" + eg + "</title>\n"
        + "<style>" + _PAGE_CSS + "</style>\n"
        + "</head>\n<body>\n"
        + '<nav class="toc">\n'
        + "<h1>Table of Contents - " + eg + "</h1>\n"
        + "<ol>\n" + toc_items + "</ol>\n</nav>\n"
        + body
        + "</body>\n</html>"
    )
