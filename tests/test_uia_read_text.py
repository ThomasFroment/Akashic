"""``WindowsUia.read_text`` asks UI Automation only for visible, non-password text nodes, so the
node cut (``max_nodes``) counts matched nodes, not the whole tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from capture_fakes import window

from akashic.capture.ports import Rect, TextBlock
from akashic.capture.text import MAX_UIA_NODES
from akashic.platform.win import uia as win_uia

if TYPE_CHECKING:
    from collections.abc import Callable

ids = win_uia.uia
SCREEN = Rect(0, 0, 1000, 800)
INSIDE = Rect(10, 10, 200, 40)
UIA_E_TIMEOUT = -2146233083

type Condition = Callable[[Node], bool]


def com_error() -> Exception:
    error: Exception = win_uia.comtypes.COMError(
        UIA_E_TIMEOUT, "UIA_E_TIMEOUT", (None, None, None, None, None)
    )
    return error


@dataclass
class Node:
    name: str = ""
    control_type: int = field(default_factory=lambda: ids.UIA_TextControlTypeId)
    offscreen: bool | None = False  # None: the provider does not report it
    password: bool = False
    value: str | None = None
    rect: Rect = INSIDE

    def prop(self, prop: int) -> object:
        if prop == ids.UIA_ControlTypePropertyId:
            return self.control_type
        if prop == ids.UIA_IsOffscreenPropertyId:
            return False if self.offscreen is None else self.offscreen  # UIA default
        if prop == ids.UIA_IsPasswordPropertyId:
            return self.password
        if prop == ids.UIA_ValueValuePropertyId:
            return self.value
        return None

    @property
    def CachedControlType(self) -> int:  # noqa: N802 - COM
        return self.control_type

    @property
    def CachedIsPassword(self) -> bool:  # noqa: N802 - COM
        return self.password

    @property
    def CachedName(self) -> str:  # noqa: N802 - COM
        return self.name

    @property
    def CachedBoundingRectangle(self) -> Rect:  # noqa: N802 - COM
        return self.rect

    def GetCachedPropertyValue(self, prop: int) -> object:  # noqa: N802 - COM
        return self.prop(prop)


class Found:
    def __init__(self, items: list[Node]) -> None:
        self.items = items

    @property
    def Length(self) -> int:  # noqa: N802 - COM
        return len(self.items)

    def GetElement(self, index: int) -> Node:  # noqa: N802 - COM
        return self.items[index]


class Root:
    def __init__(self, nodes: list[Node], *, honour_condition: bool = True) -> None:
        self.nodes = nodes
        self.honour = honour_condition
        self.conditions: list[Condition] = []

    def FindAllBuildCache(  # noqa: N802 - COM
        self, scope: int, condition: Condition, cache: object
    ) -> Found:
        del scope, cache
        self.conditions.append(condition)
        return Found([n for n in self.nodes if not self.honour or condition(n)])


class _Cache:
    def AddProperty(self, prop: int) -> None:  # noqa: N802 - COM
        del prop


class Auto:
    def __init__(self, root: Root, *, fail_on: str | None = None) -> None:
        self.root = root
        self.fail_on = fail_on
        self.true_conditions = 0

    def _maybe_fail(self, what: str) -> None:
        if self.fail_on == what:
            raise com_error()

    def ElementFromHandle(self, hwnd: int) -> Root:  # noqa: N802 - COM
        del hwnd
        return self.root

    def CreatePropertyCondition(self, prop: int, value: object) -> Condition:  # noqa: N802 - COM
        self._maybe_fail("property")
        return lambda n: n.prop(prop) == value

    def CreateAndCondition(self, a: Condition, b: Condition) -> Condition:  # noqa: N802 - COM
        self._maybe_fail("and")
        return lambda n: a(n) and b(n)

    def CreateOrCondition(self, a: Condition, b: Condition) -> Condition:  # noqa: N802 - COM
        self._maybe_fail("or")
        return lambda n: a(n) or b(n)

    def CreateTrueCondition(self) -> Condition:  # noqa: N802 - COM
        self.true_conditions += 1
        return lambda _n: True

    def CreateCacheRequest(self) -> object:  # noqa: N802 - COM
        self._maybe_fail("cache")
        return _Cache()


def reader(
    nodes: list[Node], *, honour_condition: bool = True, fail_on: str | None = None
) -> tuple[win_uia.WindowsUia, Root, Auto]:
    root = Root(nodes, honour_condition=honour_condition)
    auto = Auto(root, fail_on=fail_on)
    r = object.__new__(win_uia.WindowsUia)
    r._auto = auto  # pyright: ignore[reportAttributeAccessIssue]
    return r, root, auto


TARGET = window(rect=SCREEN)


def texts(blocks: list[TextBlock] | None) -> list[str]:
    assert blocks is not None
    return [b.text for b in blocks]


def test_only_a_filtered_condition_reaches_find_all() -> None:
    r, root, auto = reader([Node("hello")])
    assert r.read_text(TARGET, 100) == [TextBlock("hello", INSIDE, content=True)]
    assert auto.true_conditions == 0
    assert len(root.conditions) == 1
    condition = root.conditions[0]
    assert condition(Node("t"))
    assert not condition(Node("off", offscreen=True))
    assert not condition(Node("pw", control_type=ids.UIA_EditControlTypeId, password=True))
    assert not condition(Node("pane", control_type=ids.UIA_PaneControlTypeId))
    assert not condition(Node("img", control_type=ids.UIA_ImageControlTypeId))


def test_every_text_type_matches() -> None:
    nodes = [Node(f"n{t}", control_type=t) for t in sorted(win_uia._TEXT_TYPES)]
    r, _, _ = reader(nodes)
    assert texts(r.read_text(TARGET, 1000)) == [n.name for n in nodes]


def test_visible_text_after_a_long_unmatched_history_is_still_returned() -> None:
    history = [Node(f"old{i}", offscreen=True) for i in range(MAX_UIA_NODES)]
    history += [Node(f"pane{i}", control_type=ids.UIA_PaneControlTypeId) for i in range(500)]
    r, _, _ = reader([*history, Node("visible now")])
    assert r.read_text(TARGET, MAX_UIA_NODES) == [TextBlock("visible now", INSIDE, content=True)]


def test_the_cut_applies_to_matched_nodes_in_tree_order() -> None:
    nodes = [Node("noise", offscreen=True), *(Node(f"m{i}") for i in range(5))]
    r, _, _ = reader(nodes)
    assert texts(r.read_text(TARGET, 3)) == ["m0", "m1", "m2"]


def test_boundaries_of_the_cut() -> None:
    r, _, _ = reader([Node("a"), Node("b")])
    assert r.read_text(TARGET, 0) == []
    assert texts(r.read_text(TARGET, 1)) == ["a"]
    assert texts(r.read_text(TARGET, 2)) == ["a", "b"]
    assert texts(r.read_text(TARGET, 3)) == ["a", "b"]
    empty, _, _ = reader([])
    assert empty.read_text(TARGET, 10) == []


def test_realistic_max_tree_keeps_visible_text() -> None:
    nodes = [Node(f"x{i}", offscreen=True) for i in range(30_000)] + [Node("tail")]
    r, _, _ = reader(nodes)
    assert texts(r.read_text(TARGET, MAX_UIA_NODES)) == ["tail"]


def test_password_nodes_are_never_returned_even_if_the_condition_is_ignored() -> None:
    nodes = [
        Node("secret", control_type=ids.UIA_EditControlTypeId, password=True, value="hunter2"),
        Node("ok"),
    ]
    for honour in (True, False):
        r, _, _ = reader(nodes, honour_condition=honour)
        assert texts(r.read_text(TARGET, 10)) == ["ok"]


def test_a_node_without_an_offscreen_report_is_matched() -> None:
    r, _, _ = reader([Node("unreported", offscreen=None)])
    assert texts(r.read_text(TARGET, 10)) == ["unreported"]


def test_extraction_rules_are_unchanged() -> None:
    edit = Node("Label", control_type=ids.UIA_EditControlTypeId, value="typed")
    same = Node("same", control_type=ids.UIA_ComboBoxControlTypeId, value="same")
    no_name = Node("", control_type=ids.UIA_EditControlTypeId, value="only value")
    value_on_text = Node("t", value="ignored")  # Value is read for edits and combos only
    button = Node("OK", control_type=ids.UIA_ButtonControlTypeId)
    doc = Node("Title", control_type=ids.UIA_DocumentControlTypeId)
    outside = Node("far", rect=Rect(2000, 2000, 2100, 2100))
    clipped = Node("clip", rect=Rect(900, 700, 1200, 900))
    r, _, _ = reader([edit, same, no_name, value_on_text, button, doc, outside, clipped])
    assert r.read_text(TARGET, 100) == [
        TextBlock("Label", INSIDE, content=True),
        TextBlock("typed", INSIDE, content=True),
        TextBlock("same", INSIDE, content=False),
        TextBlock("only value", INSIDE, content=True),
        TextBlock("t", INSIDE, content=True),
        TextBlock("OK", INSIDE, content=False),
        TextBlock("Title", INSIDE, content=False),
        TextBlock("clip", Rect(900, 700, 1000, 800), content=True),
    ]


@pytest.mark.parametrize("stage", ["property", "or", "and", "cache"])
def test_a_failure_while_building_the_condition_returns_none(stage: str) -> None:
    r, root, _ = reader([Node("x")], fail_on=stage)
    assert r.read_text(TARGET, 10) is None
    assert root.conditions == []


def test_a_find_all_failure_returns_none() -> None:
    r, root, _ = reader([Node("x")])

    def boom(*_: object) -> Found:
        raise com_error()

    root.FindAllBuildCache = boom  # type: ignore[method-assign]
    assert r.read_text(TARGET, 10) is None
