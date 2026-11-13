# pyright: basic
"""UI Automation: focused-window text, password-field flag and browser address bar.

One ``FindAllBuildCache`` call per window fetches every descendant with its properties cached,
instead of a cross-process round trip per property (spike: 237 nodes in 76 ms). The caller
budgets and backs off per app (``capture/uia_budget.py``).

COM runs multithreaded (``coinit_flags = 0``) so it can share the process with WinRT (OCR).
Use one instance from one thread only.

Calls into a hung app fail after the connection/transaction timeouts (a COMError) and
take the readers' existing None/unknown paths instead of blocking the capture loop.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

from akashic.capture.ports import Rect, TextBlock

if TYPE_CHECKING:
    from akashic.capture.ports import BrowserFamily, WindowInfo

# COINIT_MULTITHREADED. Must be set before comtypes is first imported, hence import_module.
sys.coinit_flags = 0  # type: ignore[attr-defined]
comtypes = importlib.import_module("comtypes")
comtypes_client = importlib.import_module("comtypes.client")
comtypes_client.GetModule("UIAutomationCore.dll")
uia = importlib.import_module("comtypes.gen.UIAutomationClient")

# Control types whose Name (and for inputs, Value) is text a person would read.
_TEXT_TYPES = frozenset(
    {
        uia.UIA_TextControlTypeId,
        uia.UIA_EditControlTypeId,
        uia.UIA_DocumentControlTypeId,
        uia.UIA_ListItemControlTypeId,
        uia.UIA_HyperlinkControlTypeId,
        uia.UIA_MenuItemControlTypeId,
        uia.UIA_TabItemControlTypeId,
        uia.UIA_TreeItemControlTypeId,
        uia.UIA_DataItemControlTypeId,
        uia.UIA_ButtonControlTypeId,
        uia.UIA_ComboBoxControlTypeId,
        uia.UIA_HeaderControlTypeId,
        uia.UIA_HeaderItemControlTypeId,
        uia.UIA_TitleBarControlTypeId,
    }
)
_VALUE_TYPES = frozenset({uia.UIA_EditControlTypeId, uia.UIA_ComboBoxControlTypeId})
_CONTENT_TYPES = _TEXT_TYPES - {
    # A document's Name is its title, not text painted across its viewport. Its full-page
    # rectangle also bridges otherwise separate columns; read its descendants or use OCR.
    uia.UIA_DocumentControlTypeId,
    uia.UIA_ButtonControlTypeId,
    uia.UIA_MenuItemControlTypeId,
    uia.UIA_TabItemControlTypeId,
    uia.UIA_TreeItemControlTypeId,
    uia.UIA_ComboBoxControlTypeId,
    uia.UIA_TitleBarControlTypeId,
}
_FIREFOX_URLBAR_ID = "urlbar-input"
# Chromium's address bar (Chrome, Edge, Brave). An app, PWA or kiosk window has none, and the
# first Edit control of its page must never be mistaken for one.
_OMNIBOX_CLASS = "OmniboxViewViews"
_CHROMIUM_FRAME_CLASS = "BrowserRootView"  # its accessible name carries the private-mode marker
_MAX_OMNIBOX_CANDIDATES = 4
_MAX_ANCESTORS = 40
UIA_CONNECTION_TIMEOUT_MS = 1000
UIA_TRANSACTION_TIMEOUT_MS = 3000
_ERRORS = (comtypes.COMError, OSError, ValueError, AttributeError)


class WindowsUia:
    def __init__(self) -> None:
        self._auto = comtypes_client.CreateObject(uia.CUIAutomation8, interface=uia.IUIAutomation2)
        self._auto.ConnectionTimeout = UIA_CONNECTION_TIMEOUT_MS
        self._auto.TransactionTimeout = UIA_TRANSACTION_TIMEOUT_MS

    def _cache(self, *properties: int):  # noqa: ANN202 - COM object
        cache = self._auto.CreateCacheRequest()
        for prop in properties:
            cache.AddProperty(prop)
        return cache

    def focused_is_password(self) -> bool | None:
        """False also when nothing has focus; None when UI Automation failed (unknown)."""
        try:
            element = self._auto.GetFocusedElement()
            if not element:  # a null interface pointer: no element has the focus
                return False
            return bool(element.CurrentIsPassword)
        except _ERRORS:
            return None

    def read_text(self, window: WindowInfo, max_nodes: int) -> list[TextBlock] | None:
        try:
            root = self._auto.ElementFromHandle(window.hwnd)
            cache = self._cache(
                uia.UIA_NamePropertyId,
                uia.UIA_ControlTypePropertyId,
                uia.UIA_IsOffscreenPropertyId,
                uia.UIA_IsPasswordPropertyId,
                uia.UIA_ValueValuePropertyId,
                uia.UIA_BoundingRectanglePropertyId,
            )
            found = root.FindAllBuildCache(
                uia.TreeScope_Descendants, self._auto.CreateTrueCondition(), cache
            )
            parts: list[TextBlock] = []
            for i in range(min(found.Length, max_nodes)):
                element = found.GetElement(i)
                control_type = element.CachedControlType
                if (
                    control_type not in _TEXT_TYPES
                    or element.CachedIsOffscreen
                    or element.CachedIsPassword
                ):
                    continue
                bounds = element.CachedBoundingRectangle
                rect = Rect(bounds.left, bounds.top, bounds.right, bounds.bottom)
                rect = rect.intersect(window.rect)
                if rect is None:
                    continue
                content = control_type in _CONTENT_TYPES
                name = element.CachedName
                if name:
                    parts.append(TextBlock(name, rect, content))
                if control_type in _VALUE_TYPES:
                    value = element.GetCachedPropertyValue(uia.UIA_ValueValuePropertyId)
                    if isinstance(value, str) and value and value != name:
                        parts.append(TextBlock(value, rect, content))
        except _ERRORS:
            return None
        return parts

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        if family != "chromium":
            return None  # Firefox and Zen put the marker in the window title itself
        try:
            root = self._auto.ElementFromHandle(window.hwnd)
            condition = self._auto.CreatePropertyCondition(
                uia.UIA_ClassNamePropertyId, _CHROMIUM_FRAME_CLASS
            )
            frame = root.FindFirstBuildCache(
                uia.TreeScope_Descendants, condition, self._cache(uia.UIA_NamePropertyId)
            )
            if not frame:
                return None
            name = frame.GetCachedPropertyValue(uia.UIA_NamePropertyId)
        except _ERRORS:
            return None
        return name if isinstance(name, str) and name else None

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        try:
            root = self._auto.ElementFromHandle(window.hwnd)
            bar = self._chromium_omnibox(root) if family == "chromium" else self._firefox_bar(root)
            if not bar:
                return None
            value = bar.GetCachedPropertyValue(uia.UIA_ValueValuePropertyId)
        except _ERRORS:
            return None
        return value if isinstance(value, str) and value else None

    def _firefox_bar(self, root):  # noqa: ANN001, ANN202 - COM object
        condition = self._auto.CreatePropertyCondition(
            uia.UIA_AutomationIdPropertyId, _FIREFOX_URLBAR_ID
        )
        return root.FindFirstBuildCache(
            uia.TreeScope_Descendants, condition, self._cache(uia.UIA_ValueValuePropertyId)
        )

    def _chromium_omnibox(self, root):  # noqa: ANN001, ANN202 - COM object
        """The Edit control of class OmniboxViewViews that is not part of the page. Web content
        can carry any class name, so a candidate inside a Document is refused."""
        condition = self._auto.CreateAndCondition(
            self._auto.CreatePropertyCondition(
                uia.UIA_ControlTypePropertyId, uia.UIA_EditControlTypeId
            ),
            self._auto.CreatePropertyCondition(uia.UIA_ClassNamePropertyId, _OMNIBOX_CLASS),
        )
        found = root.FindAllBuildCache(
            uia.TreeScope_Descendants, condition, self._cache(uia.UIA_ValueValuePropertyId)
        )
        for i in range(min(found.Length, _MAX_OMNIBOX_CANDIDATES)):
            candidate = found.GetElement(i)
            if not self._inside_document(candidate):
                return candidate
        return None

    def _inside_document(self, element) -> bool:  # noqa: ANN001 - COM object
        """True when an ancestor is a web Document, and also when that cannot be established."""
        walker = self._auto.ControlViewWalker
        current = element
        for _ in range(_MAX_ANCESTORS):
            current = walker.GetParentElement(current)
            if not current:
                return False
            if current.CurrentControlType == uia.UIA_DocumentControlTypeId:
                return True
        return True
