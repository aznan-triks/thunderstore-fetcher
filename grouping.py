"""
Smart grouping that guarantees <= max_groups final files, with automatic
splitting of groups that are too large.

The splitting criterion is the **word count per file** (`max_words_per_file`),
measured on the content actually exported (README + changelog + metadata). This
is the relevant unit for a NotebookLM-style text ingestion, and it holds
regardless of the output format (PDF, Markdown, TXT).

No hardcoded values: the word limit is passed as a parameter (default value
below), itself configurable from the web interface via the job config.
"""

from collections import defaultdict
from text_builder import mod_word_count

# Default value (overridden by the job config).
# NotebookLM accepts ~500,000 words per source; we stay under that cap.
DEFAULT_MAX_WORDS_PER_FILE = 200_000

# Name of the catch-all group used when small groups need to be merged.
# Chosen so it doesn't collide with a real Thunderstore category.
_OVERFLOW_GROUP = "Misc (merged)"


def _split_by_words(mods_list: list, max_words: int) -> list[list]:
    """
    Splits a list of mods into sub-lists whose total word count stays <= max_words.
    A single mod exceeding the limit forms its own sub-group on its own (a mod's
    text is never split in two).
    """
    chunks: list[list] = []
    current: list = []
    current_words = 0
    for mod in mods_list:
        w = mod_word_count(mod)
        if current and current_words + w > max_words:
            chunks.append(current)
            current, current_words = [], 0
        current.append(mod)
        current_words += w
    if current:
        chunks.append(current)
    return chunks


def _free_name(preferred: str, used: set) -> str:
    """
    Returns `preferred` if unused, otherwise `preferred` + "_", "__", … —
    so two generated names never collapse into the same group key.
    """
    name = preferred
    while name in used:
        name += "_"
    used.add(name)
    return name


def smart_grouping(
    mods: list[dict],
    categories: list[str],
    max_groups: int = 99,
    max_words_per_file: int = DEFAULT_MAX_WORDS_PER_FILE,
) -> dict:
    """
    Returns a dict {group_name: [mod]} such that:
    - each group represents a category (or a fraction of a category)
    - no group exceeds max_words_per_file words (except a single mod that is
      too big on its own, or a forced merge due to max_groups)
    - the total number of groups does not exceed max_groups
    """
    max_words = max(1, int(max_words_per_file))

    # 1. Split by category (looked up from metadata)
    cat_map: dict[str, list] = defaultdict(list)
    for mod in mods:
        cat = mod.get("metadata", {}).get("category") or "Uncategorized"
        cat_map[cat].append(mod)

    # 2. Sort by decreasing size (keep the biggest ones distinct)
    sorted_cats = sorted(cat_map.items(), key=lambda x: len(x[1]), reverse=True)

    # 3. Split categories that are too large (by word count)
    groups: dict[str, list] = {}
    # Reserve every real category name up front: a generated part name must
    # never steal the key of a category that is still to be assigned
    # (e.g. splitting "Mods" produces "Mods_part1", which could collide with a
    # real category literally called "Mods_part1"). Collisions used to silently
    # overwrite one of the two groups, dropping its mods.
    used: set = set(cat_map)
    for cat, mods_list in sorted_cats:
        chunks = _split_by_words(mods_list, max_words)
        if len(chunks) == 1:
            groups[cat] = chunks[0]
        else:
            for i, chunk in enumerate(chunks, start=1):
                name = _free_name("{}_part{}".format(cat, i), used)
                groups[name] = chunk

    # 4. If still > max_groups, keep the LARGEST distinct groups and merge the small ones
    if len(groups) > max_groups:
        # Sort by DECREASING size to keep the biggest ones first
        sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
        new_groups: dict[str, list] = {}
        other_pool: list = []
        for name, mods_list in sorted_groups:
            if len(new_groups) < max_groups - 1:
                new_groups[name] = mods_list
            else:
                other_pool.extend(mods_list)
        if other_pool:
            # Avoid overwriting a real category named like the catch-all group
            overflow_name = _free_name(_OVERFLOW_GROUP, set(new_groups))
            new_groups[overflow_name] = other_pool
        return new_groups

    return groups
