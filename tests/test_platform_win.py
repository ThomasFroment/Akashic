"""Windows adapters against scripted OS objects: UI Automation, hosted apps, media, audio.

Nothing here touches a real window, COM server or audio device; the capture rules built on these
adapters are tested in test_exclusions.py and test_capture_privacy.py.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from capture_fakes import AUMID, window

from akashic.capture.ports import Rect
from akashic.platform.win import audio as win_audio
from akashic.platform.win import displays as win_displays
from akashic.platform.win import media as win_media
from akashic.platform.win import uia as win_uia
from akashic.platform.win import windows as win_windows

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from akashic.capture.ports import TopWindow, WindowFacts, WindowSource


def _per_hwnd(value: object) -> Callable[[int], object]:
    def call(_hwnd: int) -> object:
        return value

    return call


def com_error(hresult: int, text: str) -> Exception:
    """A comtypes COMError. comtypes is reached through the platform module, which sets the
    COM apartment before comtypes is first imported (importing it here first would not)."""
    error: Exception = win_uia.comtypes.COMError(hresult, text, (None, None, None, None, None))
    return error


# --- UI Automation: errors fail closed ------------------------------------------------------


def test_no_reader_asks_for_the_focused_password_field() -> None:
    from akashic.capture.ports import TextSource  # noqa: PLC0415

    assert not hasattr(win_uia.WindowsUia, "focused_is_password")
    assert not hasattr(TextSource, "focused_is_password")


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
    assert sessions.active_processes() == {}


def test_a_failure_while_walking_one_device_is_unknown_for_the_whole_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = win_audio.WindowsAudioSessions()
    monkeypatch.setattr(sessions, "_refresh_managers", lambda: None)
    sessions._managers = [("id-a", "a"), ("id-b", "b")]

    def audible(manager: str) -> set[str]:
        if manager == "b":
            raise com_error(-2147023174, "RPC_S_SERVER_UNAVAILABLE")
        return {"discord.exe"}

    monkeypatch.setattr(sessions, "_audible", audible)
    assert sessions.active_processes() is None  # not {"discord.exe"}: device b is unknown


class _Endpoint:
    def __init__(self, device_id: str, names: set[str], *, fails: bool = False) -> None:
        self.device_id = device_id
        self.names = names
        self.fails = fails

    def Activate(self, *_args: object) -> _Endpoint:  # noqa: N802 - the COM method name
        if self.fails:
            raise com_error(-2004287480, "AUDCLNT_E_DEVICE_INVALIDATED")
        return self

    def QueryInterface(self, _iid: object) -> _Endpoint:  # noqa: N802
        return self


class _Endpoints:
    def __init__(self, items: list[_Endpoint]) -> None:
        self.items = items
        self.enumerations = 0

    def EnumAudioEndpoints(self, *_args: object) -> _Endpoints:  # noqa: N802
        self.enumerations += 1
        return self

    def GetCount(self) -> int:  # noqa: N802
        return len(self.items)

    def Item(self, i: int) -> _Endpoint:  # noqa: N802
        return self.items[i]


def _endpoints(monkeypatch: pytest.MonkeyPatch, *items: _Endpoint) -> _Endpoints:
    enumerator = _Endpoints(list(items))
    monkeypatch.setattr(win_audio, "_enumerator", lambda: enumerator)

    def device_id(item: _Endpoint) -> str:
        return item.device_id

    def audible(_sessions: object, manager: _Endpoint) -> set[str]:
        return manager.names

    monkeypatch.setattr(win_audio, "_device_id", device_id)
    monkeypatch.setattr(win_audio.WindowsAudioSessions, "_audible", audible)
    return enumerator


def test_audible_processes_are_keyed_by_endpoint_id_and_a_failing_endpoint_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _endpoints(
        monkeypatch,
        _Endpoint("a", {"game.exe"}),
        _Endpoint("b", set()),
        _Endpoint("c", {"x.exe"}, fails=True),
    )
    result = win_audio.WindowsAudioSessions().active_processes()
    assert result == {"a": frozenset({"game.exe"}), "b": frozenset()}


def test_refresh_reenumerates_inside_the_reuse_window_and_nothing_else_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enumerator = _endpoints(monkeypatch, _Endpoint("a", set()))
    sessions = win_audio.WindowsAudioSessions()
    sessions.active_processes()
    sessions.active_processes()
    assert enumerator.enumerations == 1
    enumerator.items.append(_Endpoint("b", {"discord.exe"}))
    assert sessions.active_processes() == {"a": frozenset()}
    assert sessions.active_processes(refresh=True) == {
        "a": frozenset(),
        "b": frozenset({"discord.exe"}),
    }
    assert enumerator.enumerations == 2


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
    assert windows._identity(7)[1] == "dashlane.exe"
    hosted[0] = None
    assert windows._identity(7)[1] == "applicationframehost.exe"


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


# --- D3D exclusive-fullscreen state (#150) ---------------------------------------------------


class _FakeShell32:
    def __init__(self, *results: tuple[int, int] | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    def SHQueryUserNotificationState(self, ref: object) -> int:  # noqa: N802
        self.calls += 1
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        hr, state = result
        ref._obj.value = state  # type: ignore[attr-defined]
        return hr


def _d3d_rig(
    monkeypatch: pytest.MonkeyPatch, shell: _FakeShell32
) -> tuple[win_windows.Win32Windows, list[float]]:
    now = [100.0]
    monkeypatch.setattr(win_windows, "shell32", shell)
    monkeypatch.setattr(win_windows.time, "monotonic", lambda: now[0])
    windows = win_windows.Win32Windows()
    rect = win_windows.Rect(0, 0, 800, 600)
    info = win_windows.WindowInfo(7, "game.exe", "T", rect, has_frame=False)
    monkeypatch.setattr(windows, "_info", _per_hwnd(info))
    monkeypatch.setattr(win_windows.user32, "GetForegroundWindow", lambda: 7, raising=False)
    monkeypatch.setattr(win_windows.user32, "IsIconic", _per_hwnd(0), raising=False)
    return windows, now


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ((0, 3), True),
        ((0, 2), False),
        ((0, 4), False),
        ((0, 0), False),
        ((0, 7), False),
        ((-2147467259, 3), False),  # failed HRESULT with a stale state value
        ((1, 3), False),  # S_FALSE is not S_OK
    ],
)
def test_only_an_ok_d3d_fullscreen_state_sets_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    result: tuple[int, int],
    expected: bool,  # noqa: FBT001
) -> None:
    windows, _ = _d3d_rig(monkeypatch, _FakeShell32(result))
    focused = windows.focused_window()
    assert focused is not None
    assert focused.exclusive_fullscreen is expected


def test_a_raising_shell_query_reports_false_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows, _ = _d3d_rig(monkeypatch, _FakeShell32(OSError("boom")))
    focused = windows.focused_window()
    assert focused is not None
    assert focused.exclusive_fullscreen is False


def test_the_shell_query_runs_at_most_once_a_second(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _FakeShell32((0, 3), (0, 2))
    windows, now = _d3d_rig(monkeypatch, shell)
    for _ in range(5):
        focused = windows.focused_window()
        assert focused is not None
        assert focused.exclusive_fullscreen is True
        now[0] += 0.1
    assert shell.calls == 1
    now[0] += 1.0
    focused = windows.focused_window()
    assert focused is not None
    assert focused.exclusive_fullscreen is False
    assert shell.calls == 2


def test_no_focused_window_means_no_shell_query(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _FakeShell32((0, 3))
    windows, _ = _d3d_rig(monkeypatch, shell)
    monkeypatch.setattr(windows, "_info", _per_hwnd(None))
    assert windows.focused_window() is None
    assert shell.calls == 0


# --- every visible top-level window with its facts ---------------------------------------------

_CAPTION = 0x00C00000
_POPUP = 0x80000000
_TOOL = 0x80
_LAYERED = 0x80000
_FRAME = "ApplicationFrameWindow"
_CORE = "Windows.UI.Core.CoreWindow"


@dataclass
class _Win:
    pid: int = 10
    cls: str = "Plain"
    title: str = ""
    visible: bool = True
    dwm_rect: tuple[int, int, int, int] | None = (0, 0, 100, 50)
    rect: tuple[int, int, int, int] | None = (1, 1, 99, 49)
    style: int = _CAPTION
    exstyle: int = 0
    iconic: bool = False
    cloak: int | None = 0  # None: the DWM call fails
    owner: int = 0
    layered: tuple[int, int] | None = None  # (alpha, flags); None: the call fails
    hung: bool = False
    children: tuple[int, ...] = ()
    ghost_of: int = 0


class _DeskUser32:
    def __init__(self, order: list[int], wins: dict[int, _Win], *, enum_ok: bool = True) -> None:
        self.order, self.wins, self.enum_ok = order, wins, enum_ok

    def EnumWindows(self, proc: object, param: int) -> int:  # noqa: N802
        for hwnd in self.order:
            proc(hwnd, param)  # type: ignore[operator]
        return int(self.enum_ok)

    def EnumChildWindows(self, hwnd: int, proc: object, param: int) -> None:  # noqa: N802
        # Like the real call: every descendant, depth first, stopping when the callback says so.
        for child in self.wins[hwnd].children:
            if not proc(child, param):  # type: ignore[operator]
                return
            self.EnumChildWindows(child, proc, param)

    def IsWindowVisible(self, hwnd: int) -> int:  # noqa: N802
        return int(self.wins[hwnd].visible)

    def IsIconic(self, hwnd: int) -> int:  # noqa: N802
        return int(self.wins[hwnd].iconic)

    def IsHungAppWindow(self, hwnd: int) -> int:  # noqa: N802
        return int(self.wins[hwnd].hung)

    def GetWindow(self, hwnd: int, kind: int) -> int:  # noqa: N802
        assert kind == 4  # GW_OWNER
        return self.wins[hwnd].owner

    def GetWindowRect(self, hwnd: int, ref: object) -> int:  # noqa: N802
        rect = self.wins[hwnd].rect
        if rect is None:
            return 0
        out = ref._obj  # type: ignore[attr-defined]
        out.left, out.top, out.right, out.bottom = rect
        return 1

    def GetWindowTextLengthW(self, hwnd: int) -> int:  # noqa: N802
        return len(self.wins[hwnd].title)

    def GetWindowTextW(self, hwnd: int, buffer: ctypes.Array[ctypes.c_wchar], size: int) -> int:  # noqa: N802
        buffer.value = self.wins[hwnd].title[: size - 1]
        return len(buffer.value)

    def GetClassNameW(self, hwnd: int, buffer: ctypes.Array[ctypes.c_wchar], size: int) -> int:  # noqa: N802
        buffer.value = self.wins[hwnd].cls[: size - 1]
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd: int, pid: object) -> int:  # noqa: N802
        pid._obj.value = self.wins[hwnd].pid  # type: ignore[attr-defined]
        return 1

    def GetWindowLongW(self, hwnd: int, index: int) -> int:  # noqa: N802
        win = self.wins[hwnd]
        return win.style if index == -16 else win.exstyle

    def GetLayeredWindowAttributes(  # noqa: N802
        self, hwnd: int, key: object, alpha: object, flags: object
    ) -> int:
        del key
        layered = self.wins[hwnd].layered
        if layered is None:
            return 0
        alpha._obj.value, flags._obj.value = layered  # type: ignore[attr-defined]
        return 1


class _DeskDwm:
    def __init__(self, wins: dict[int, _Win]) -> None:
        self.wins = wins

    def DwmGetWindowAttribute(self, hwnd: int, attr: int, ref: object, size: int) -> int:  # noqa: N802
        del size
        win = self.wins[hwnd]
        out = ref._obj  # type: ignore[attr-defined]
        if attr == 9:  # DWMWA_EXTENDED_FRAME_BOUNDS
            if win.dwm_rect is None:
                return 0x80070057
            out.left, out.top, out.right, out.bottom = win.dwm_rect
            return 0
        assert attr == 14  # DWMWA_CLOAKED
        if win.cloak is None:
            return 0x80070057
        out.value = win.cloak
        return 0


_NAMES = {10: "app.exe", 20: "other.exe", 30: "dwm.exe", 100: "applicationframehost.exe"}


def _desk(
    monkeypatch: pytest.MonkeyPatch,
    wins: dict[int, _Win],
    order: list[int] | None = None,
    *,
    names: dict[int, str] | None = None,
    enum_ok: bool = True,
) -> win_windows.Win32Windows:
    monkeypatch.setattr(
        win_windows,
        "user32",
        _DeskUser32(list(wins) if order is None else order, wins, enum_ok=enum_ok),
    )
    monkeypatch.setattr(win_windows, "dwmapi", _DeskDwm(wins))
    table = _NAMES if names is None else names

    def image_name(pid: int) -> str:
        return table.get(pid, "")

    monkeypatch.setattr(win_windows, "image_name", image_name)
    monkeypatch.setattr(win_windows, "_hung_window_from_ghost", None)
    return win_windows.Win32Windows()


def _listed(
    monkeypatch: pytest.MonkeyPatch, wins: dict[int, _Win], **kwargs: object
) -> dict[int, TopWindow]:
    listed = _desk(monkeypatch, wins, **kwargs).windows()  # type: ignore[arg-type]
    assert listed is not None
    return {w.hwnd: w for w in listed}


def test_win32_windows_still_serves_both_window_ports() -> None:
    both: WindowSource = win_windows.Win32Windows()
    facts: WindowFacts = win_windows.Win32Windows()
    assert both is not None
    assert facts is not None


def test_every_visible_window_is_listed_in_enum_order_and_hidden_ones_never(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wins = {
        1: _Win(),
        2: _Win(visible=False),
        3: _Win(iconic=True, cloak=1, exstyle=_TOOL, dwm_rect=(-32000, -32000, -31840, -31972)),
        4: _Win(dwm_rect=(0, 0, 0, 0)),
        5: _Win(dwm_rect=(99999, 99999, 100100, 100050)),
    }
    listed = _desk(monkeypatch, wins, [5, 3, 2, 4, 1]).windows()
    assert listed is not None
    assert [w.hwnd for w in listed] == [5, 3, 4, 1]


def test_a_failed_enumeration_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _desk(monkeypatch, {1: _Win()}, enum_ok=False).windows() is None


def test_rect_is_the_dwm_frame_with_get_window_rect_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = _listed(monkeypatch, {1: _Win(), 2: _Win(dwm_rect=None)})
    assert listed[1].rect == Rect(0, 0, 100, 50)
    assert listed[2].rect == Rect(1, 1, 99, 49)


def test_a_window_that_vanished_is_left_out_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = _listed(monkeypatch, {1: _Win(dwm_rect=None, rect=None), 2: _Win()})
    assert list(listed) == [2]


def test_class_title_minimized_cloaked_and_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {
        1: _Win(cls="Chrome_WidgetWin_1", title="Doc é", iconic=True, cloak=2, owner=7),
        2: _Win(cloak=None),
    }
    listed = _listed(monkeypatch, wins)
    one = listed[1]
    assert (one.class_name, one.title) == ("Chrome_WidgetWin_1", "Doc é")
    assert (one.minimized, one.cloaked, one.owner) == (True, True, 7)
    two = listed[2]
    assert (two.minimized, two.cloaked, two.owner) == (False, False, None)


def test_popup_tool_and_frame_come_from_the_styles(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {
        1: _Win(style=_POPUP | _CAPTION, exstyle=_TOOL),
        2: _Win(style=0x00800000),  # border only: not all of WS_CAPTION
        3: _Win(style=0),
    }
    listed = _listed(monkeypatch, wins)
    assert (listed[1].popup, listed[1].tool, listed[1].has_frame) == (True, True, True)
    assert (listed[2].popup, listed[2].tool, listed[2].has_frame) == (False, False, False)
    assert listed[3].has_frame is False


def test_alpha_only_when_layered_with_lwa_alpha(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {
        1: _Win(exstyle=_LAYERED, layered=(0, 2)),
        2: _Win(exstyle=_LAYERED, layered=(128, 2)),
        3: _Win(exstyle=_LAYERED, layered=(255, 1)),  # colour key only
        4: _Win(exstyle=_LAYERED, layered=None),  # call fails
        5: _Win(layered=(128, 2)),  # not layered
    }
    listed = _listed(monkeypatch, wins)
    assert [listed[h].alpha for h in (1, 2, 3, 4, 5)] == [0, 128, None, None, None]


def test_pid_and_process_of_the_window_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {1: _Win(pid=10), 2: _Win(pid=999), 3: _Win(pid=0)}
    listed = _listed(monkeypatch, wins)
    assert (listed[1].pid, listed[1].process, listed[1].uwp_unresolved) == (10, "app.exe", False)
    assert (listed[2].pid, listed[2].process) == (999, "")
    assert (listed[3].pid, listed[3].process) == (0, "")


def test_a_frame_host_resolves_to_its_core_window_app(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {
        1: _Win(pid=100, cls=_FRAME, children=(2, 3)),
        2: _Win(pid=100, cls="ApplicationFrameInputSinkWindow"),
        3: _Win(pid=20, cls=_CORE),
        # a frame whose app name cannot be read
        4: _Win(pid=100, cls=_FRAME, children=(5,)),
        5: _Win(pid=999, cls=_CORE),
        # a frame with no CoreWindow child
        6: _Win(pid=100, cls=_FRAME, children=(2,)),
    }
    listed = _listed(monkeypatch, wins, order=[1, 4, 6])
    assert (listed[1].pid, listed[1].process, listed[1].uwp_unresolved) == (20, "other.exe", False)
    assert (listed[4].pid, listed[4].process, listed[4].uwp_unresolved) == (999, "", False)
    assert (listed[6].pid, listed[6].process, listed[6].uwp_unresolved) == (
        100,
        "applicationframehost.exe",
        True,
    )


def test_foreign_children_are_sorted_unique_names(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {
        1: _Win(pid=10, children=(2, 3, 4, 5, 6)),
        2: _Win(pid=10),
        3: _Win(pid=20),
        4: _Win(pid=20),
        5: _Win(pid=30, visible=False),  # hidden children count too
        6: _Win(pid=10, children=(7,)),
        7: _Win(pid=40),  # a descendant, not only a direct child
        8: _Win(pid=10),
    }
    names = {**_NAMES, 40: "a.exe"}
    listed = _listed(monkeypatch, wins, order=[1, 8], names=names)
    assert listed[1].foreign_children == ("a.exe", "dwm.exe", "other.exe")
    assert listed[8].foreign_children == ()


def test_an_unnameable_foreign_child_makes_the_list_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wins = {1: _Win(pid=10, children=(2, 3)), 2: _Win(pid=20), 3: _Win(pid=999)}
    assert _listed(monkeypatch, wins, order=[1])[1].foreign_children is None


def test_a_resolved_frame_does_not_count_its_app_or_host_children_as_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wins = {
        1: _Win(pid=100, cls=_FRAME, children=(2, 3, 4)),
        2: _Win(pid=100, cls="ApplicationFrameInputSinkWindow"),
        3: _Win(pid=20, cls=_CORE),
        4: _Win(pid=10),  # a real foreign child still counts
    }
    listed = _listed(monkeypatch, wins, order=[1])
    assert listed[1].foreign_children == ("app.exe",)
    wins[4].pid = 20
    assert _listed(monkeypatch, wins, order=[1])[1].foreign_children == ()


def test_ghost_by_class_or_by_a_hung_window_of_dwm(monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {
        1: _Win(cls="Ghost", pid=10),
        2: _Win(pid=30, hung=True),
        3: _Win(pid=30, hung=False),
        4: _Win(pid=10, hung=True),  # a hung app window that is not the ghost
        5: _Win(pid=10),
    }
    listed = _listed(monkeypatch, wins)
    assert [listed[h].is_ghost for h in (1, 2, 3, 4, 5)] == [True, True, False, False, False]


def test_ghost_of_is_set_only_for_a_ghost_with_a_known_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wins = {
        1: _Win(cls="Ghost", ghost_of=55),
        2: _Win(cls="Ghost", ghost_of=0),
        3: _Win(ghost_of=55),  # not a ghost
    }
    windows = _desk(monkeypatch, wins)

    def hung_window(hwnd: int) -> int:
        return wins[hwnd].ghost_of

    monkeypatch.setattr(win_windows, "_hung_window_from_ghost", hung_window)
    listed = {w.hwnd: w for w in windows.windows() or []}
    assert [listed[h].ghost_of for h in (1, 2, 3)] == [55, None, None]
    monkeypatch.setattr(win_windows, "_hung_window_from_ghost", None)
    assert {w.hwnd: w.ghost_of for w in windows.windows() or []} == {1: None, 2: None, 3: None}


def test_a_hosted_app_with_an_unreadable_name_reports_an_empty_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wins = {
        1: _Win(pid=100, cls=_FRAME, children=(2,)),
        2: _Win(pid=999, cls=_CORE),
    }
    listed = _listed(monkeypatch, wins)
    assert listed[1].process == ""


# --- DPI awareness ------------------------------------------------------------------------------


def test_set_dpi_aware_sets_per_monitor_v2_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []

    class _User32:
        def SetProcessDpiAwarenessContext(self, context: object) -> int:  # noqa: N802
            seen.append(getattr(context, "value", None))
            return 1

    monkeypatch.setattr(win_displays, "user32", _User32())
    assert win_displays.set_dpi_aware() is None
    assert seen == [ctypes.c_void_p(-4).value]


def test_build_capture_platform_sets_dpi_awareness_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from akashic.platform import win  # noqa: PLC0415 - Windows only

    calls: list[str] = []

    class _Stop(Exception):  # noqa: N818
        pass

    def aware() -> None:
        calls.append("dpi")

    def displays() -> object:
        calls.append("displays")
        raise _Stop

    monkeypatch.setattr(win, "set_dpi_aware", aware)
    monkeypatch.setattr(win, "Win32Displays", displays)
    with pytest.raises(_Stop):
        win.build_capture_platform("en")
    assert calls[0] == "dpi"
