"""Every Console board panel must exist.

Confirmed live, and it took the whole app down: the Console's "Alarm
History" panel was renamed to "Events" in ConsolePage.jsx, but
boardConfig.js still listed the board item id `alarmHistory`. The board's
renderItem looked the panel up by id, got undefined, and read `.label` off
it - a TypeError during render. The Console is the landing page, so every
route rendered white with nothing but a console error to say why.

Two halves to the fix, and this is the static one: the ids must match.
(The other half is in ConsolePage.jsx - an unknown id now costs one panel,
not the page.) Parsing the JSX is deliberate: the ids live in two files
that must agree, and nothing else was checking that they did.
"""
import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "frontend" / "src"


def _board_ids():
    text = (SRC / "boardConfig.js").read_text()
    listed = re.search(r"BOARD_ITEM_IDS = \[(.*?)\]", text, re.S).group(1)
    titled = re.search(r"BOARD_ITEM_TITLES = \{(.*?)\n\}", text, re.S).group(1)
    defaults = re.search(r"DEFAULT_BOARD_ITEMS = \[(.*?)\n\]", text, re.S).group(1)
    return (set(re.findall(r'"([A-Za-z]+)"', listed)),
            set(re.findall(r"^\s*([A-Za-z]+):", titled, re.M)),
            set(re.findall(r'id: "([A-Za-z]+)"', defaults)))


def _panel_ids():
    """The panels array in ConsolePage.jsx: `{ id: "x", label: "Y", ... }`."""
    text = (SRC / "ConsolePage.jsx").read_text()
    panels = text[text.index("const panels = ["):]
    return set(re.findall(r'id: "([A-Za-z]+)",\s*\n\s*label: "', panels))


def test_every_board_item_has_a_panel_behind_it():
    ids, _, _ = _board_ids()
    missing = ids - _panel_ids()
    assert not missing, f"board ids with no panel in ConsolePage.jsx: {sorted(missing)} - the Console renders white"


# `history` (Command History) has been defined in ConsolePage.jsx and
# absent from boardConfig.js since the board landed (3305656), so the
# board has never been able to show it. Found by this test; left alone
# because making it appear is a UI decision, not a bug fix. Listed here
# so it stays visible instead of being absorbed into a passing test.
KNOWN_UNREACHABLE = {"history"}


def test_every_panel_is_placeable_on_the_board():
    ids, _, _ = _board_ids()
    orphans = _panel_ids() - ids - KNOWN_UNREACHABLE
    assert not orphans, f"panels no board item can show: {sorted(orphans)}"


def test_the_three_board_lists_agree():
    listed, titled, defaults = _board_ids()
    assert listed == titled == defaults, (
        f"BOARD_ITEM_IDS/BOARD_ITEM_TITLES/DEFAULT_BOARD_ITEMS disagree: "
        f"{sorted(listed ^ titled)} {sorted(listed ^ defaults)}"
    )


def test_a_missing_panel_degrades_to_one_panel_not_the_page():
    """The runtime half: renderItem must not assume the lookup found one."""
    text = (SRC / "ConsolePage.jsx").read_text()
    render = text[text.index("renderItem={(item) =>"):]
    render = render[:render.index("/>")]
    assert "panel ?" in render or "panel &&" in render, "renderItem dereferences the panel without checking it exists"
