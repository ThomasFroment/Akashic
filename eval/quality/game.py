"""Synthetic Direct3D 11 game: borderless or exclusive fullscreen, frame times, HUD change tags.

    python game.py --mode borderless|exclusive --rect X Y W H --seconds N --log game.jsonl
                   [--sound SCHEDULE.json --device NAME]

Run under a renamed interpreter so its sound is a game process's. Plain ctypes vtable calls, no
akashic import. Both modes call MakeWindowAssociation(DXGI_MWA_NO_ALT_ENTER). Borderless: WS_POPUP
over the rect, FLIP_DISCARD; with tearing support it presents with ALLOW_TEARING, else
Present(0, 0) (recorded). Exclusive: SetFullscreenState at the current mode, Present(0, 0).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import random
import sys
import threading
import time
from ctypes import POINTER, byref, c_float, c_int, c_uint, c_void_p, wintypes
from pathlib import Path

import numpy as np

import tags
from common import JsonlWriter

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
d3d11 = ctypes.WinDLL("d3d11")

IID_IDXGIDevice = (0x54EC77FA, 0x1377, 0x44E6, bytes.fromhex("8c3288fd5f44c84c"))
IID_IDXGIFactory = (0x7B7166EC, 0x21C7, 0x44AE, bytes.fromhex("b21ac9ae321ae369"))
IID_IDXGIFactory5 = (0x7632E1F5, 0xEE65, 0x4DCA, bytes.fromhex("87fd84cd75f8838d"))
IID_ID3D11Texture2D = (0x6F15AAF2, 0xD208, 0x4E89, bytes.fromhex("9ab4489535d34f9c"))
FORMAT_BGRA = 87
DXGI_MWA_NO_ALT_ENTER = 2
DXGI_STATUS_OCCLUDED = 0x087A0001
DXGI_PRESENT_TEST = 0x1
PRESENT_ALLOW_TEARING = 0x200
SWAP_ALLOW_TEARING = 0x800
FEATURE_PRESENT_ALLOW_TEARING = 0
HUD_PERIOD_S = 10.0  # assumed: one in-game text event (objective, kill feed, chat line) every 10 s
HUD_CELL = 8
HUD_TAG_BASE = 0x800000  # above every id the site hands out


class GUID(ctypes.Structure):
    _fields_ = (("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8))

    def __init__(self, t) -> None:
        super().__init__(t[0], t[1], t[2], (ctypes.c_ubyte * 8)(*t[3]))


class Rational(ctypes.Structure):
    _fields_ = (("n", c_uint), ("d", c_uint))


class ModeDesc(ctypes.Structure):
    _fields_ = (("w", c_uint), ("h", c_uint), ("refresh", Rational), ("fmt", c_uint), ("order", c_uint), ("scaling", c_uint))


class SwapDesc(ctypes.Structure):
    _fields_ = (("mode", ModeDesc), ("count", c_uint), ("quality", c_uint), ("usage", c_uint), ("buffers", c_uint),
                ("hwnd", wintypes.HWND), ("windowed", wintypes.BOOL), ("effect", c_uint), ("flags", c_uint))


class Box(ctypes.Structure):
    _fields_ = tuple((n, c_uint) for n in ("left", "top", "front", "right", "bottom", "back"))


def vcall(obj: int, slot: int, *args):
    """Call COM method `slot` of `obj`. args are (ctype, value) pairs; returns an HRESULT / int."""
    vtable = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *[t for t, _ in args])(vtable[slot])
    return fn(obj, *[v for _, v in args])


def com_out(obj: int, slot: int, *args) -> int:
    out = c_void_p()
    hr = vcall(obj, slot, *args, (POINTER(c_void_p), byref(out)))
    if hr < 0:
        raise OSError(f"COM call slot {slot} failed: 0x{hr & 0xFFFFFFFF:08x}")
    return out.value


def query(obj: int, iid) -> int:
    return com_out(obj, 0, (POINTER(GUID), byref(GUID(iid))))


class WndClass(ctypes.Structure):
    _fields_ = (("style", c_uint), ("proc", c_void_p), ("cls", c_int), ("wnd", c_int), ("inst", wintypes.HINSTANCE),
                ("icon", c_void_p), ("cursor", c_void_p), ("bg", c_void_p), ("menu", ctypes.c_wchar_p), ("name", ctypes.c_wchar_p))


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, c_uint, wintypes.WPARAM, wintypes.LPARAM)


class Msg(ctypes.Structure):
    _fields_ = (("hwnd", wintypes.HWND), ("msg", c_uint), ("w", wintypes.WPARAM), ("l", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT))


def make_window(x: int, y: int, w: int, h: int, keep: list, on_activate) -> int:
    user32.DefWindowProcW.argtypes = (wintypes.HWND, c_uint, wintypes.WPARAM, wintypes.LPARAM)
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = (wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD, c_int, c_int, c_int, c_int, wintypes.HWND, c_void_p, wintypes.HINSTANCE, c_void_p)

    def proc(hwnd, msg, wp, lp):
        if msg == 0x2:  # WM_DESTROY
            user32.PostQuitMessage(0)
        elif msg == 0x1C and wp:  # WM_ACTIVATEAPP, activated
            on_activate()
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    keep.append(WNDPROC(proc))
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    cls = WndClass(proc=ctypes.cast(keep[-1], c_void_p), inst=kernel32.GetModuleHandleW(None), name="QualityGame")
    user32.RegisterClassW(byref(cls))
    hwnd = user32.CreateWindowExW(0, "QualityGame", "Quality Game", 0x80000000 | 0x10000000, x, y, w, h, None, None, cls.inst, None)  # WS_POPUP | WS_VISIBLE
    user32.SetForegroundWindow(hwnd)
    return hwnd


class Game:
    def __init__(self, mode: str, rect: tuple[int, int, int, int], log: JsonlWriter) -> None:
        self.mode, self.log = mode, log
        self.keep: list = []
        x, y, w, h = rect
        self.w, self.h = w, h
        self.occluded = False
        self.regain = False  # set when the app is activated again: an exclusive game goes back to fullscreen
        self.hwnd = make_window(x, y, w, h, self.keep, lambda: setattr(self, "regain", True))
        device, self.ctx = c_void_p(), c_void_p()
        hr = d3d11.D3D11CreateDevice(None, 1, None, 0x20, None, 0, 7, byref(device), None, byref(self.ctx))
        if hr < 0:
            raise OSError(f"D3D11CreateDevice 0x{hr & 0xFFFFFFFF:08x}")
        self.device = device.value
        dxgi_device = query(self.device, IID_IDXGIDevice)
        adapter = com_out(dxgi_device, 7)
        factory = com_out(adapter, 6, (POINTER(GUID), byref(GUID(IID_IDXGIFactory))))
        self.tearing = self._tearing_supported(factory)
        flags = SWAP_ALLOW_TEARING if (self.tearing and mode == "borderless") else 0
        desc = SwapDesc(ModeDesc(w, h, Rational(0, 1), FORMAT_BGRA, 0, 0), 1, 0, 0x20, 2, self.hwnd, True, 4, flags)  # FLIP_DISCARD, RENDER_TARGET_OUTPUT
        self.swap = com_out(factory, 10, (c_void_p, self.device), (POINTER(SwapDesc), byref(desc)))
        vcall(factory, 8, (wintypes.HWND, self.hwnd), (c_uint, DXGI_MWA_NO_ALT_ENTER))
        if mode == "exclusive":
            vcall(self.swap, 10, (wintypes.BOOL, 1), (c_void_p, None))
        self.present_flags = PRESENT_ALLOW_TEARING if flags else 0
        self._acquire_buffers()
        log.write(type="game_start", mode=mode, tearing_supported=self.tearing, present_flags=self.present_flags,
                  swap_flags=flags, w=w, h=h, hwnd=int(self.hwnd or 0))
        self.state = self._real_mode()
        log.write(type="mode_change", state=self.state)

    def _acquire_buffers(self) -> None:
        self.back = com_out(self.swap, 9, (c_uint, 0), (POINTER(GUID), byref(GUID(IID_ID3D11Texture2D))))
        self.rtv = com_out(self.device, 9, (c_void_p, self.back), (c_void_p, None))

    def _real_mode(self) -> str:
        """What the swap chain is really in: DXGI leaves exclusive fullscreen when the window loses focus."""
        if self.mode == "borderless":
            return "borderless"
        fullscreen = wintypes.BOOL(0)
        vcall(self.swap, 11, (POINTER(wintypes.BOOL), byref(fullscreen)), (c_void_p, None))
        return "exclusive" if fullscreen.value else "windowed"

    def _reenter_exclusive(self) -> None:
        """Back to exclusive fullscreen after a focus loss; the buffers are released and fetched again around ResizeBuffers."""
        vcall(self.swap, 10, (wintypes.BOOL, 1), (c_void_p, None))
        vcall(self.rtv, 2)
        vcall(self.back, 2)
        vcall(self.swap, 13, (c_uint, 0), (c_uint, 0), (c_uint, 0), (c_uint, 0), (c_uint, 0))
        self._acquire_buffers()

    @staticmethod
    def _tearing_supported(factory: int) -> bool:
        try:
            f5 = query(factory, IID_IDXGIFactory5)
        except OSError:
            return False
        allow = c_int(0)
        vcall(f5, 28, (c_uint, FEATURE_PRESENT_ALLOW_TEARING), (POINTER(c_int), byref(allow)), (c_uint, 4))
        return bool(allow.value)

    def set_hud(self, tag_id: int) -> None:
        gray = tags.encode(tag_id, HUD_CELL)
        self.hud = np.ascontiguousarray(np.dstack([gray, gray, gray, np.full_like(gray, 255)]))

    def draw_hud(self) -> None:
        h, w = self.hud.shape[:2]
        box = Box(0, 0, 0, w, h, 1)
        vcall(self.ctx.value, 48, (c_void_p, self.back), (c_uint, 0), (POINTER(Box), byref(box)), (c_void_p, self.hud.ctypes.data), (c_uint, w * 4), (c_uint, 0))

    def run(self, seconds: float) -> None:
        msg = Msg()
        end = time.monotonic() + seconds
        last = time.perf_counter_ns()
        frame_ms: list[float] = []
        sec_start, tag_id, next_hud = time.monotonic(), HUD_TAG_BASE, 0.0
        hue = 0.0
        while time.monotonic() < end:
            while user32.PeekMessageW(byref(msg), None, 0, 0, 1):
                if msg.msg == 0x12:  # WM_QUIT
                    return
                user32.DispatchMessageW(byref(msg))
            now = time.monotonic()
            if self.mode == "exclusive" and self.regain:
                self.regain = False
                self._reenter_exclusive()
            state = self._real_mode()
            if state != self.state:
                self.state = state
                self.log.write(type="mode_change", state=state)
            if self.occluded:  # hidden: no frames, only a test present to see when it is visible again
                self.occluded = vcall(self.swap, 8, (c_uint, 0), (c_uint, DXGI_PRESENT_TEST)) == DXGI_STATUS_OCCLUDED
                last = time.perf_counter_ns()
                sec_start, frame_ms = time.monotonic(), []
                time.sleep(0.01)
                continue
            hue = (hue + 0.002) % 1.0
            vcall(self.ctx.value, 50, (c_void_p, self.rtv), (c_float * 4, (c_float * 4)(hue, 0.2, 1 - hue, 1.0)))
            if now >= next_hud:
                tag_id += 1
                next_hud = now + HUD_PERIOD_S
                self.set_hud(tag_id)
                changed = True
            else:
                changed = False
            self.draw_hud()
            self.occluded = vcall(self.swap, 8, (c_uint, 0), (c_uint, self.present_flags)) == DXGI_STATUS_OCCLUDED
            if changed:
                self.log.write(type="change", kind="hud", tag=tag_id)
            t = time.perf_counter_ns()
            frame_ms.append((t - last) / 1e6)
            last = t
            if now - sec_start >= 1.0:
                a = np.array(frame_ms)
                self.log.write(type="frames", fps=len(a) / (now - sec_start), p50=float(np.percentile(a, 50)), p99=float(np.percentile(a, 99)), max=float(a.max()), mode=self.state)
                frame_ms, sec_start = [], now
        if self.mode == "exclusive":
            vcall(self.swap, 10, (wintypes.BOOL, 0), (c_void_p, None))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("borderless", "exclusive"), required=True)
    ap.add_argument("--rect", nargs=4, type=int, required=True)
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--sound", type=Path)
    ap.add_argument("--device", default="")
    args = ap.parse_args()
    user32.SetProcessDpiAwarenessContext(c_void_p(-4))
    log = JsonlWriter(args.log)
    if args.sound:
        import player

        schedule = json.loads(args.sound.read_text(encoding="utf-8"))
        threading.Thread(target=player.play, args=(schedule, args.device, JsonlWriter(args.log.with_name("player-game.jsonl"))), daemon=True).start()
    game = Game(args.mode, tuple(args.rect), log)
    game.run(args.seconds)
    log.write(type="game_end")
    return 0


if __name__ == "__main__":
    random.seed(0)
    sys.exit(main())
