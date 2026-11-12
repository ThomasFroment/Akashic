"""Layout/content regressions using invented screen text."""

from akashic.capture.ports import Rect, TextBlock
from akashic.capture.text import assemble_text


def test_interface_clutter_does_not_count_as_content() -> None:
    controls = [TextBlock("Settings home preferences search contacts channels", content=False)]
    assert assemble_text(controls) == ""
    assert assemble_text([*controls, TextBlock("Le rendez-vous est à dix-neuf heures.")]) == (
        "Le rendez-vous est à dix-neuf heures."
    )


def test_columns_are_read_separately_and_repeated_messages_survive() -> None:
    blocks = [
        TextBlock("Heading", Rect(0, 0, 600, 20)),
        TextBlock("Left first", Rect(0, 40, 120, 60)),
        TextBlock("Right first", Rect(360, 40, 500, 60)),
        TextBlock("Encore", Rect(0, 70, 120, 90)),
        TextBlock("Right last", Rect(360, 70, 500, 90)),
        TextBlock("Encore", Rect(0, 100, 120, 120)),
    ]
    text = assemble_text(blocks)
    assert text.index("Left first") < text.index("Encore") < text.index("Right first")
    assert text.count("Encore") == 2
    assert "\n\nRight first" in text


def test_duplicate_nodes_are_removed_but_paragraphs_are_preserved() -> None:
    first = TextBlock("The game\nreleases Friday.", Rect(0, 0, 200, 30))
    second = TextBlock("Le mod arrive demain.", Rect(0, 80, 200, 100))
    assert assemble_text([first, first, second]) == (
        "The game\nreleases Friday.\n\nLe mod arrive demain."
    )


def test_browser_chrome_beside_wide_heading_does_not_merge_page_columns() -> None:
    # Geometry observed in the native Zen fixture: the sidebar address overlaps the heading's
    # vertical band, preventing the old single-heading shortcut from separating the columns.
    blocks = [
        TextBlock("fixture address", Rect(12, 68, 252, 91)),
        TextBlock("Mémoire locale", Rect(299, 68, 1552, 118)),
        TextBlock("Gauche", Rect(276, 191, 869, 234)),
        TextBlock("Minlo propose samedi.", Rect(276, 263, 706, 294)),
        TextBlock("Encore demain.", Rect(276, 326, 749, 357)),
        TextBlock("Encore demain.", Rect(276, 388, 692, 419)),
        TextBlock("Droite", Rect(981, 191, 1574, 234)),
        TextBlock("The studio is available.", Rect(981, 263, 1441, 294)),
        TextBlock("Do not book it yet.", Rect(981, 326, 1493, 357)),
    ]
    text = assemble_text(blocks)
    assert text.index("Mémoire locale") < text.index("Gauche")
    assert text.rindex("Encore demain.") < text.index("Droite")
    assert text.count("Encore demain.") == 2


def test_wide_footer_does_not_interleave_columns_above_it() -> None:
    blocks = [
        TextBlock("Left first", Rect(0, 0, 140, 20)),
        TextBlock("Right first", Rect(400, 0, 540, 20)),
        TextBlock("Left last", Rect(0, 40, 140, 60)),
        TextBlock("Right last", Rect(400, 40, 540, 60)),
        TextBlock("Full-width footer", Rect(0, 140, 600, 160)),
    ]
    text = assemble_text(blocks)
    assert text.index("Left last") < text.index("Right first")
    assert text.index("Right last") < text.index("Full-width footer")
