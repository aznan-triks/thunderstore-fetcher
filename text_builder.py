"""
Generates the text exports (Markdown and TXT) from mod data.

Unlike html_builder (which produces HTML destined for WeasyPrint -> PDF), this
module produces readable text directly:
  - Markdown: keeps the original markup of the README/changelogs, strips images
    (useless for a NotebookLM-style text ingestion), keeps links.
  - TXT: a "flattened" version with no Markdown markup.

Word counting (`count_words`) is shared with grouping (grouping.py) so that the
per-file split reflects the volume actually exported.
"""
import re
from html_builder import remove_images_and_links

# Markdown image: ![alt](url) — removed everywhere (no value in plain text)
_IMG_RE = re.compile(r'!\[.*?\]\(.*?\)')


def _remove_images(md_text: str) -> str:
    return _IMG_RE.sub('', md_text or "")


def count_words(text: str) -> int:
    """Word count of a text (split on whitespace)."""
    return len((text or "").split())


def mod_word_count(mod: dict) -> int:
    """
    Word volume of a mod as it will be exported: README + changelog + the small
    amount of textual metadata. Used for the word-limit-based splitting.
    """
    md = mod.get("metadata", {}) or {}
    deps = md.get("dependencies", [])
    deps_txt = " ".join(deps) if isinstance(deps, list) else str(deps or "")
    blobs = [
        mod.get("readme") or "",
        mod.get("changelog") or "",
        str(md.get("version", "")),
        str(md.get("author", "")),
        deps_txt,
    ]
    return sum(count_words(b) for b in blobs)


def _meta_lines(metadata: dict) -> list[str]:
    deps_raw = metadata.get("dependencies", [])
    deps = ", ".join(deps_raw) if isinstance(deps_raw, list) else str(deps_raw or "")
    deps = deps or "None"
    downloads = "{:,}".format(metadata.get("downloads", 0))
    return [
        "- **Version**: {}".format(metadata.get("version", "N/A")),
        "- **Author**: {}".format(metadata.get("author", "Unknown")),
        "- **Downloads**: {}".format(downloads),
        "- **Category**: {}".format(metadata.get("category", "Uncategorized")),
        "- **Dependencies**: {}".format(deps),
    ]


# ── Markdown ──────────────────────────────────────────────────────────────────

def build_group_markdown(group_name: str, mods_data: list) -> str:
    out: list[str] = ["# {}".format(group_name or "Unnamed"), ""]

    # Table of contents
    out += ["## Table of contents", ""]
    for mod in mods_data:
        out.append("- {}".format(mod["name"]))
    out.append("")

    for mod in mods_data:
        out += ["---", "", "# {}".format(mod["name"]), ""]
        out += _meta_lines(mod.get("metadata", {}))
        out.append("")

        readme = _remove_images(mod.get("readme") or "").strip()
        out += ["## README", "", readme or "_No README available._", ""]

        changelog = _remove_images(mod.get("changelog") or "").strip()
        if changelog:
            out += ["## Changelog", "", changelog, ""]

    return "\n".join(out)


# ── TXT (flattened Markdown) ─────────────────────────────────────────────────

_RE_FENCE   = re.compile(r'^```.*$', re.MULTILINE)        # code block fences
_RE_HEADING = re.compile(r'^#{1,6}\s*', re.MULTILINE)     # # Headings
_RE_QUOTE   = re.compile(r'^>\s?', re.MULTILINE)          # > quotes
_RE_BULLET  = re.compile(r'^\s{0,3}[-*+]\s+', re.MULTILINE)
_RE_EMPH    = re.compile(r'(\*\*|__|\*|_|`)')             # bold / italic / inline code
_RE_BLANKS  = re.compile(r'\n{3,}')                       # collapse blank lines


def _md_to_text(md_text: str) -> str:
    t = remove_images_and_links(md_text or "")  # images removed, links -> text
    t = _RE_FENCE.sub('', t)
    t = _RE_HEADING.sub('', t)
    t = _RE_QUOTE.sub('', t)
    t = _RE_BULLET.sub('• ', t)
    t = _RE_EMPH.sub('', t)
    t = _RE_BLANKS.sub('\n\n', t)
    return t.strip()


def build_group_txt(group_name: str, mods_data: list) -> str:
    sep = "=" * 70
    out: list[str] = [group_name or "Unnamed", sep, "", "TABLE OF CONTENTS", ""]
    for mod in mods_data:
        out.append("  - {}".format(mod["name"]))
    out.append("")

    for mod in mods_data:
        md = mod.get("metadata", {})
        deps_raw = md.get("dependencies", [])
        deps = ", ".join(deps_raw) if isinstance(deps_raw, list) else str(deps_raw or "")
        out += [
            sep, "", mod["name"], "",
            "Version: {}".format(md.get("version", "N/A")),
            "Author: {}".format(md.get("author", "Unknown")),
            "Downloads: {:,}".format(md.get("downloads", 0)),
            "Category: {}".format(md.get("category", "Uncategorized")),
            "Dependencies: {}".format(deps or "None"),
            "",
            "README",
            "------",
        ]
        readme = _md_to_text(mod.get("readme") or "")
        out += [readme or "No README available.", ""]

        changelog = _md_to_text(mod.get("changelog") or "")
        if changelog:
            out += ["CHANGELOG", "---------", changelog, ""]

    return "\n".join(out)
