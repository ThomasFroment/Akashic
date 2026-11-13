"""Windows adapters against scripted OS objects: UI Automation, hosted apps, media, audio.

Nothing here touches a real window, COM server or audio device; the capture rules built on these
adapters are tested in test_exclusions.py and test_capture_privacy.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from capture_fakes import AUMID, window

from akashic.platform.win import audio as win_audio
from akashic.platform.win import media as win_media
from akashic.platform.win import uia as win_uia
from akashic.platform.win import windows as win_windows

if TYPE_CHECKING:
    import ctypes
    from collections.abc import Callable, Iterator


def com_error(hresult: int, text: str) -> Exception:
    """A comtypes COMError. comtypes is reached through the platform module, which sets the
    COM apartment before comtypes is first imported (importing it here first would not)."""
    error: Exception = win_uia.comtypes.COMError(hresult, text, (None, None, None, None, None))
    return error


# --- UI Automation: errors fail closed ------------------------------------------------------


class _BrokenAutomation:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def GetFocusedElement(self) -> object:  # noqa: N802 - the COM method name
        raise self.error


@pytest.mark.parametrize(
    "error",
    [com_error(-2147220991, "UIA_E_ELEMENTNOTAVAILABLE"), OSError("rpc failed")],
    ids=["com", "os"],
)
def test_password_check_reports_unknown_on_ui_automation_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    reader = object.__new__(win_uia.WindowsUia)  # no COM object: only the error path runs
    monkeypatch.setattr(reader, "_auto", _BrokenAutomation(error), raising=False)
    assert reader.focused_is_password() is None  # unknown, never "not a password field"


# --- UI Automation: the address bar is the omnibox, not an in-page input --------------------


@dataclass
class _Element:
    props: dict[int, object]
    children: list[_Element]

    def walk(self) -> Iterator[_Element]:
        for child in self.children:
            yield child
            yield from child.walk()

    def FindFirstBuildCache(  # noqa: N802 - COM
        self, scope: int, condition: Condition, cache: object
    ) -> _Element | None:
        del scope, cache
        return next((e for e in self.walk() if condition(e)), None)

    def FindFirst(self, scope: int, condition: Condition) -> _Element | None:  # noqa: N802 - COM
        return self.FindFirstBuildCache(scope, condition, None)

    def FindAllBuildCache(  # noqa: N802 - COM
        self, scope: int, condition: Condition, cache: object
    ) -> _Found:
        del scope, cache
        return _Found([e for e in self.walk() if condition(e)])

    @property
    def CurrentControlType(self) -> object:  # noqa: N802 - COM
        return self.props.get(win_uia.uia.UIA_ControlTypePropertyId)

    def GetCachedPropertyValue(self, prop: int) -> object:  # noqa: N802 - COM
        return self.props.get(prop)

    def GetCurrentPropertyValue(self, prop: int) -> object:  # noqa: N802 - COM
        return self.props.get(prop)


type Condition = Callable[[_Element], bool]


class _Found:
    def __init__(self, items: list[_Element]) -> None:
        self.items = items

    @property
    def Length(self) -> int:  # noqa: N802 - COM
        return len(self.items)

    def GetElement(self, index: int) -> _Element:  # noqa: N802 - COM
        return self.items[index]


class _Walker:
    def __init__(self, root: _Element) -> None:
        self.root = root

    def GetParentElement(self, element: _Element) -> _Element | None:  # noqa: N802 - COM
        return next(
            (e for e in [self.root, *self.root.walk()] if any(c is element for c in e.children)),
            None,
        )


class _FakeAutomation:
    """Enough of IUIAutomation to search a scripted element tree by property conditions."""

    def __init__(self, root: _Element) -> None:
        self.root = root

    def ElementFromHandle(self, hwnd: int) -> _Element:  # noqa: N802 - COM
        del hwnd
        return self.root

    @property
    def ControlViewWalker(self) -> _Walker:  # noqa: N802 - COM
        return _Walker(self.root)

    def CreatePropertyCondition(self, prop: int, value: object) -> Condition:  # noqa: N802 - COM
        return lambda e: e.props.get(prop) == value

    def CreateAndCondition(self, a: Condition, b: Condition) -> Condition:  # noqa: N802 - COM
        return lambda e: a(e) and b(e)

    def CreateOrCondition(self, a: Condition, b: Condition) -> Condition:  # noqa: N802 - COM
        return lambda e: a(e) or b(e)

    def CreateNotCondition(self, a: Condition) -> Condition:  # noqa: N802 - COM
        return lambda e: not a(e)

    def CreateTrueCondition(self) -> Condition:  # noqa: N802 - COM
        return bool  # every element is truthy

    def CreateCacheRequest(self) -> _Cache:  # noqa: N802 - COM
        return _Cache()


class _Cache:
    def AddProperty(self, prop: int) -> None:  # noqa: N802 - COM
        del prop


def _edit(value: str, class_name: str = "") -> _Element:
    ids = win_uia.uia
    return _Element(
        {
            ids.UIA_ControlTypePropertyId: ids.UIA_EditControlTypeId,
            ids.UIA_ClassNamePropertyId: class_name,
            ids.UIA_ValueValuePropertyId: value,
        },
        [],
    )


def _pane(control_type: int, *children: _Element) -> _Element:
    return _Element({win_uia.uia.UIA_ControlTypePropertyId: control_type}, list(children))


def test_chromium_url_comes_from_the_omnibox_never_from_an_in_page_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = win_uia.uia
    page_input = _edit("my typed secret")
    omnibox = _edit("https://example.org/page", "OmniboxViewViews")
    browser = _pane(
        ids.UIA_WindowControlTypeId,
        _pane(ids.UIA_DocumentControlTypeId, page_input),
        _pane(ids.UIA_ToolBarControlTypeId, omnibox),
    )
    kiosk = _pane(ids.UIA_WindowControlTypeId, _pane(ids.UIA_DocumentControlTypeId, page_input))
    reader = object.__new__(win_uia.WindowsUia)
    target = window(process="chrome.exe", title="Page - Google Chrome")
    monkeypatch.setattr(reader, "_auto", _FakeAutomation(browser), raising=False)
    assert reader.read_url(target, "chromium") == "https://example.org/page"
    monkeypatch.setattr(reader, "_auto", _FakeAutomation(kiosk), raising=False)
    assert reader.read_url(target, "chromium") is None


def _reader_for(monkeypatch: pytest.MonkeyPatch, root: _Element) -> win_uia.WindowsUia:
    reader = object.__new__(win_uia.WindowsUia)
    monkeypatch.setattr(reader, "_auto", _FakeAutomation(root), raising=False)
    return reader


SPOOFED = "https://www.paypal.com/in-page-text"


def test_chromium_page_content_cannot_pose_as_the_omnibox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Web content can carry any class name, so an Edit inside the Document is refused even
    when it is called OmniboxViewViews; a PWA or kiosk window then has no address at all."""
    ids = win_uia.uia
    spoof = _edit(SPOOFED, "OmniboxViewViews")
    pwa = _pane(ids.UIA_WindowControlTypeId, _pane(ids.UIA_DocumentControlTypeId, spoof))
    target = window(process="chrome.exe", title="App - Google Chrome")
    assert _reader_for(monkeypatch, pwa).read_url(target, "chromium") is None
    real = _edit("https://example.org/real", "OmniboxViewViews")
    browser = _pane(
        ids.UIA_WindowControlTypeId,
        _pane(ids.UIA_DocumentControlTypeId, spoof),
        _pane(ids.UIA_ToolBarControlTypeId, real),
    )
    assert _reader_for(monkeypatch, browser).read_url(target, "chromium") == (
        "https://example.org/real"
    )


def test_chromium_frame_caption_is_read_from_the_browser_frame_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = win_uia.uia
    frame = _Element(
        {
            ids.UIA_ControlTypePropertyId: ids.UIA_CustomControlTypeId,
            ids.UIA_ClassNamePropertyId: "BrowserRootView",
            ids.UIA_NamePropertyId: "Page - Google Chrome (Incognito)",
        },
        [],
    )
    tree = _pane(ids.UIA_WindowControlTypeId, frame)
    target = window(process="chrome.exe", title="Page - Google Chrome")
    assert _reader_for(monkeypatch, tree).browser_caption(target, "chromium") == (
        "Page - Google Chrome (Incognito)"
    )
    assert _reader_for(monkeypatch, tree).browser_caption(target, "firefox") is None
    bare = _pane(ids.UIA_WindowControlTypeId)
    assert _reader_for(monkeypatch, bare).browser_caption(target, "chromium") is None


def test_firefox_address_comes_from_the_url_bar_id(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = win_uia.uia
    bar = _Element(
        {
            ids.UIA_ControlTypePropertyId: ids.UIA_EditControlTypeId,
            ids.UIA_AutomationIdPropertyId: "urlbar-input",
            ids.UIA_ValueValuePropertyId: "example.org/page",
        },
        [],
    )
    tree = _pane(ids.UIA_WindowControlTypeId, _pane(ids.UIA_DocumentControlTypeId, _edit("x")), bar)
    target = window(process="firefox.exe", title="Page - Firefox")
    assert _reader_for(monkeypatch, tree).read_url(target, "firefox") == "example.org/page"


def test_negative_control_the_first_edit_in_tree_order_reads_page_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = win_uia.uia

    def first_edit(self: win_uia.WindowsUia, root: _Element) -> _Element | None:
        condition = self._auto.CreatePropertyCondition(  # pyright: ignore[reportAttributeAccessIssue]
            ids.UIA_ControlTypePropertyId, ids.UIA_EditControlTypeId
        )
        return root.FindFirstBuildCache(ids.TreeScope_Descendants, condition, None)

    monkeypatch.setattr(win_uia.WindowsUia, "_chromium_omnibox", first_edit)
    pwa = _pane(ids.UIA_WindowControlTypeId, _pane(ids.UIA_DocumentControlTypeId, _edit(SPOOFED)))
    target = window(process="chrome.exe", title="App - Google Chrome")
    assert _reader_for(monkeypatch, pwa).read_url(target, "chromium") == SPOOFED


def test_uia_distinguishes_unknown_no_focus_and_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Focused:
        def __init__(self, element: object) -> None:
            self.element = element

        def GetFocusedElement(self) -> object:  # noqa: N802 - the COM method name
            return self.element

    class _Field:
        def __init__(self, *, password: bool) -> None:
            self.CurrentIsPassword = password

    reader = object.__new__(win_uia.WindowsUia)
    for element, expected in [
        (None, False),  # a null interface pointer: nothing has the focus
        (_Field(password=False), False),
        (_Field(password=True), True),
    ]:
        monkeypatch.setattr(reader, "_auto", _Focused(element), raising=False)
        assert reader.focused_is_password() is expected


# --- audio sessions: unknown is not silence ---------------------------------------------------


def test_a_com_error_is_unknown_and_nothing_audible_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = win_audio.WindowsAudioSessions()

    def broken() -> None:
        raise com_error(-2004287480, "AUDCLNT_E_DEVICE_INVALIDATED")

    monkeypatch.setattr(sessions, "_refresh_managers", broken)
    assert sessions.active_processes() is None
    monkeypatch.setattr(sessions, "_refresh_managers", lambda: (_ for _ in ()).throw(OSError("x")))
    assert sessions.active_processes() is None
    monkeypatch.setattr(sessions, "_refresh_managers", lambda: None)
    sessions._managers = []
    assert sessions.active_processes() == frozenset()


def test_a_failure_while_walking_one_device_is_unknown_for_the_whole_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = win_audio.WindowsAudioSessions()
    monkeypatch.setattr(sessions, "_refresh_managers", lambda: None)
    sessions._managers = ["a", "b"]

    def audible(manager: str) -> set[str]:
        if manager == "b":
            raise com_error(-2147023174, "RPC_S_SERVER_UNAVAILABLE")
        return {"discord.exe"}

    monkeypatch.setattr(sessions, "_audible", audible)
    assert sessions.active_processes() is None  # not {"discord.exe"}: device b is unknown


# --- hosted (UWP) apps: the real process behind the frame host -------------------------------


class _FakeUser32:
    """EnumChildWindows over scripted children: handle -> (class name, pid)."""

    def __init__(self, children: dict[int, tuple[str, int]]) -> None:
        self.children = children

    def EnumChildWindows(self, frame: int, proc: object, param: int) -> None:  # noqa: N802
        del frame
        for handle in self.children:
            if not proc(handle, param):  # type: ignore[operator]
                break

    def GetClassNameW(self, handle: int, buffer: ctypes.Array[ctypes.c_wchar], size: int) -> int:  # noqa: N802
        buffer.value = self.children[handle][0][: size - 1]  # the API truncates to the buffer
        return len(buffer.value)

    def GetWindowThreadProcessId(self, handle: int, pid: object) -> int:  # noqa: N802
        pid._obj.value = self.children[handle][1]  # type: ignore[attr-defined]
        return 1


def test_the_core_window_pid_behind_the_frame_host_gives_the_real_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = {
        10: ("ApplicationFrameInputSinkWindow", 100),  # the host's own helper window
        11: ("Windows.UI.Core.CoreWindow", 100),  # (not the app: same pid as the host)
        12: ("Windows.UI.Core.CoreWindow", 4242),  # the app's core window
    }
    monkeypatch.setattr(win_windows, "user32", _FakeUser32(children))
    assert win_windows.Win32Windows._hosted_pid(1, 100) == 4242
    assert win_windows.Win32Windows._hosted_pid(1, 4242) == 100
    monkeypatch.setattr(win_windows, "user32", _FakeUser32({10: ("Other", 7)}))
    assert win_windows.Win32Windows._hosted_pid(1, 100) is None


def test_a_hosted_app_is_named_by_its_own_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = win_windows.Win32Windows()
    names = {100: "applicationframehost.exe", 4242: "dashlane.exe"}
    hosted: list[int | None] = [4242]

    def hosted_pid(frame: int, host_pid: int) -> int | None:
        del frame, host_pid
        return hosted[0]

    monkeypatch.setattr(win_windows, "image_name", names.get)
    monkeypatch.setattr(win_windows.Win32Windows, "_hosted_pid", staticmethod(hosted_pid))

    def pid_of(hwnd: int, ref: object) -> int:
        del hwnd
        ref._obj.value = 100  # type: ignore[attr-defined]
        return 1

    monkeypatch.setattr(win_windows.user32, "GetWindowThreadProcessId", pid_of, raising=False)
    assert windows._process_name(7) == "dashlane.exe"
    hosted[0] = None
    assert windows._process_name(7) == "applicationframehost.exe"


# --- media app names ---------------------------------------------------------------------------


class _Names:
    """Scripted app listings for WindowsMedia: each call returns the next result."""

    def __init__(self, *results: dict[str, str] | None) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self) -> dict[str, str] | None:
        self.calls += 1
        return self.results.pop(0) if self.results else {}


def test_the_platform_does_not_remember_a_failed_app_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    media = win_media.WindowsMedia(clock=lambda: now[0])
    names = _Names(None, {"Other": "Other app"}, {"Other": "Other app", AUMID: "Firefox"})
    monkeypatch.setattr(media, "_load_names", names)
    assert media.app_name(AUMID) is None  # listing failed
    now[0] += 5
    assert media.app_name(AUMID) is None
    assert names.calls == 1  # too soon to list again
    now[0] += win_media.NAMES_RELOAD_S
    assert media.app_name(AUMID) is None  # listed, but the app is not there yet
    now[0] += win_media.NAMES_RELOAD_S
    assert media.app_name(AUMID) == "Firefox"  # installed or started since
    calls = names.calls
    assert media.app_name(AUMID) == "Firefox"
    assert names.calls == calls  # a name that is known never triggers a listing


# --- UI Automation: a hung app times out instead of blocking ---------------------------------

UIA_E_TIMEOUT = -2146233083  # 0x80131505


class _TimingOutAutomation:
    """Every call into the hung app fails with UIA_E_TIMEOUT, as the transaction timeout does."""

    def __init__(self) -> None:
        self.calls = 0

    def _timeout(self) -> Exception:
        self.calls += 1
        return com_error(UIA_E_TIMEOUT, "UIA_E_TIMEOUT")

    def GetFocusedElement(self) -> object:  # noqa: N802 - COM
        raise self._timeout()

    def ElementFromHandle(self, hwnd: int) -> object:  # noqa: N802 - COM
        del hwnd
        raise self._timeout()


class _FakeComObject:
    def __init__(self) -> None:
        self.ConnectionTimeout = None
        self.TransactionTimeout = None


def test_windows_uia_uses_the_v8_automation_object_with_named_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[object, object]] = []
    fake = _FakeComObject()

    def create(clsid: object, interface: object = None) -> _FakeComObject:
        created.append((clsid, interface))
        return fake

    monkeypatch.setattr(win_uia.comtypes_client, "CreateObject", create)
    reader = win_uia.WindowsUia()
    assert created == [(win_uia.uia.CUIAutomation8, win_uia.uia.IUIAutomation2)]
    assert reader._auto is fake
    assert fake.ConnectionTimeout == win_uia.UIA_CONNECTION_TIMEOUT_MS
    assert fake.TransactionTimeout == win_uia.UIA_TRANSACTION_TIMEOUT_MS
    # Milliseconds, bounded: a hung app must not block the capture loop for long.
    assert 0 < win_uia.UIA_CONNECTION_TIMEOUT_MS <= win_uia.UIA_TRANSACTION_TIMEOUT_MS <= 10_000


def test_a_timed_out_password_check_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = object.__new__(win_uia.WindowsUia)
    monkeypatch.setattr(reader, "_auto", _TimingOutAutomation(), raising=False)
    assert reader.focused_is_password() is None


def test_timed_out_readers_take_their_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = object.__new__(win_uia.WindowsUia)
    auto = _TimingOutAutomation()
    monkeypatch.setattr(reader, "_auto", auto, raising=False)
    target = window(process="chrome.exe", title="Page - Google Chrome")
    assert reader.read_text(target, 100) is None
    assert reader.read_url(target, "chromium") is None
    assert reader.read_url(target, "firefox") is None
    assert reader.browser_caption(target, "chromium") is None
    assert auto.calls == 4  # each reader made its one failing call and gave up
