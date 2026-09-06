"""Tests for the smart grouping logic."""
import grouping as g


def _mod(name, category, words=20):
    readme = " ".join(["word"] * words)
    return {
        "name": name,
        "metadata": {"category": category, "version": "1", "author": "a",
                     "downloads": 1, "dependencies": []},
        "readme": readme,
        "changelog": "",
    }


def _words_of(group):
    return sum(len(m["readme"].split()) for m in group)


def test_groups_by_category_within_word_limit():
    mods = [_mod("A", "Cat1"), _mod("B", "Cat1"), _mod("C", "Cat2")]
    groups = g.smart_grouping(mods, [], max_groups=99, max_words_per_file=100)
    assert set(groups) == {"Cat1", "Cat2"}


def test_large_category_is_split_into_parts():
    mods = [_mod("M{}".format(i), "Big", words=10) for i in range(10)]
    groups = g.smart_grouping(mods, [], max_groups=99, max_words_per_file=25)
    parts = sorted(k for k in groups if k.startswith("Big"))
    # 5 chunks of 2 mods (20 words each); single-chunk categories are kept whole
    assert parts == ["Big_part{}".format(i) for i in range(1, 6)]
    assert len(groups) == 5
    # no group exceeds the limit, no mod lost
    assert all(_words_of(v) <= 25 for k, v in groups.items())
    assert sum(len(v) for v in groups.values()) == 10


def test_part_name_collides_with_real_category_name():
    """Splitting "Mods" must not silently overwrite a real category literally
    called "Mods_part1" (both used to map to the same group key)."""
    big = [_mod("Big{}".format(i), "Mods", words=10) for i in range(6)]
    real = [_mod("Real1", "Mods_part1", words=5)]
    tiny = [_mod("Tiny", "Mods_part2", words=5)]
    groups = g.smart_grouping(
        big + real + tiny, [], max_groups=99, max_words_per_file=30)
    # The real categories keep their own groups; their mods are intact.
    assert len(groups.get("Mods_part1", [])) == 1
    assert len(groups.get("Mods_part2", [])) == 1
    # "Mods" was split under keys that never steal a real category's name:
    # "Mods_part1"/"Mods_part2" are taken -> the parts become "Mods_part1_",
    # "Mods_part2_" and the free "Mods_part3".
    split_keys = [k for k in groups if k.startswith("Mods_part")
                  and k not in ("Mods_part1", "Mods_part2")]
    assert sorted(split_keys) == ["Mods_part1_", "Mods_part2_", "Mods_part3"]
    assert sum(len(groups[k]) for k in split_keys) == 6
    assert sum(len(v) for v in groups.values()) == 8  # nothing dropped


def test_max_groups_merges_small_groups():
    mods = [_mod("M{}".format(i), "Category{}".format(i % 6), words=5) for i in range(18)]
    groups = g.smart_grouping(mods, [], max_groups=3, max_words_per_file=1000)
    assert len(groups) <= 3
    assert sum(len(v) for v in groups.values()) == 18
    # biggest category stayed distinct
    assert any(k == "Category0" for k in groups)


def test_overflow_group_name_does_not_collide_with_real_category():
    mods = [_mod("M{}".format(i), "Misc (merged)", words=5) for i in range(4)] + \
           [_mod("X{}".format(i), "Other{}".format(i), words=5) for i in range(4)]
    groups = g.smart_grouping(mods, [], max_groups=1, max_words_per_file=1000)
    assert len(groups) == 1
    name = next(iter(groups))
    assert name.startswith("Misc (merged)")
    assert sum(len(v) for v in groups.values()) == 8


def test_empty_input():
    assert g.smart_grouping([], [], max_groups=5) == {}
