"""User-takeover detection that tells the runner's own input apart from the user's.

Why: the first live runs aborted with "mouse moved by user" although nobody touched the mouse. The old detector
compared the cursor position with where the runner had put it, which fails as soon as the game (or Windows)
moves the cursor itself (cursor warping / recentring, clipping, DPI rounding).

How: Windows low-level hooks (WH_MOUSE_LL, WH_KEYBOARD_LL) see every input event before any window does, with
an `injected` flag and the sender's dwExtraInfo. The runner tags every event it sends with RUNNER_TAG, so:
  * ours        = injected and dwExtraInfo == RUNNER_TAG      -> ignored
  * physical    = not injected (a real mouse / keyboard)       -> takeover
  * foreign injected key / button (another program)            -> takeover
  * foreign injected cursor MOVE (e.g. a tool or driver)       -> counted and reported, not a takeover
Cursor warps done by the game with SetCursorPos are not input events at all, so they can never trigger it.
Detection is armed only while the runner owns the game (after focus is stable); it is disarmed during console
prompts and manual setup. F12 / Pause seen by the hook latch the emergency stop (in addition to polling).
Hooks only observe: every event is passed on unchanged (CallNextHookEx).
"""
import math
import threading
import time

RUNNER_TAG = 0x0D9A7E57                 # dwExtraInfo on every SendInput event of the runner
MOVE_TAKEOVER_PX = 6                    # cumulative physical cursor travel that counts as a takeover
EMERGENCY_VKS = (0x7B, 0x13)            # F12, Pause

WM_MOUSEMOVE = 0x0200
MOUSE_MSG_NAMES = {0x0201: 'left button', 0x0202: 'left button up', 0x0204: 'right button', 0x0205: 'right button up',
                   0x0207: 'middle button', 0x0208: 'middle button up', 0x020A: 'wheel', 0x020E: 'horizontal wheel',
                   0x020B: 'x button', 0x020C: 'x button up'}
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104


class TakeoverMonitor:
    """Pure logic (unit-tested). Fed by the hook thread on Windows, or directly by tests / the mock."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.armed = False
        self.reason = None
        self.emergency = False
        self.travel = 0.0
        self.last_pt = None
        self.counts = {'ours': 0, 'physical_moves': 0, 'foreign_injected_moves': 0, 'physical_keys': 0,
                       'foreign_other': 0}

    def arm(self):
        self.reason, self.travel, self.armed = None, 0.0, True

    def disarm(self):
        self.armed = False

    def on_mouse(self, msg, x, y, injected, ours):
        prev, self.last_pt = self.last_pt, (x, y)
        if ours:
            self.counts['ours'] += 1
            return
        if msg == WM_MOUSEMOVE:
            if injected:
                self.counts['foreign_injected_moves'] += 1
                return
            self.counts['physical_moves'] += 1
            if self.armed and prev is not None:
                self.travel += math.hypot(x - prev[0], y - prev[1])
                if self.travel >= MOVE_TAKEOVER_PX and not self.reason:
                    self.reason = 'physical mouse movement (%.0f px)' % self.travel
            return
        self.counts['foreign_other'] += 1
        if self.armed and not self.reason:
            self.reason = '%s mouse %s' % ('injected (another program)' if injected else 'physical',
                                          MOUSE_MSG_NAMES.get(msg, 'event 0x%X' % msg))

    def on_key(self, vk, down, injected, ours):
        if ours or not down:
            return
        if vk in EMERGENCY_VKS:
            self.emergency = True
            return
        self.counts['physical_keys' if not injected else 'foreign_other'] += 1
        if self.armed and not self.reason:
            self.reason = '%s key press (virtual key 0x%02X)' % ('injected (another program)' if injected
                                                                 else 'physical', vk)

    def takeover(self):
        return self.reason if self.armed else None


class LowLevelHooks:
    """Windows only: installs WH_MOUSE_LL + WH_KEYBOARD_LL on a dedicated thread with a message loop."""
    def __init__(self, monitor):
        self.monitor = monitor
        self.ok = False
        self.error = None
        self._tid = None
        self._ready = threading.Event()
        self._th = None

    def start(self, timeout_s=2.0):
        self._th = threading.Thread(target=self._run, name='input-hooks', daemon=True)
        self._th.start()
        self._ready.wait(timeout_s)
        return self.ok

    def _run(self):
        import ctypes
        import ctypes.wintypes as wt
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        ULONG_PTR = ctypes.c_size_t

        class MSLL(ctypes.Structure):
            _fields_ = [('pt', wt.POINT), ('mouseData', wt.DWORD), ('flags', wt.DWORD), ('time', wt.DWORD),
                        ('dwExtraInfo', ULONG_PTR)]

        class KBDLL(ctypes.Structure):
            _fields_ = [('vkCode', wt.DWORD), ('scanCode', wt.DWORD), ('flags', wt.DWORD), ('time', wt.DWORD),
                        ('dwExtraInfo', ULONG_PTR)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        u32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
        u32.SetWindowsHookExW.restype = ctypes.c_void_p
        u32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        u32.CallNextHookEx.restype = LRESULT
        u32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        k32.GetModuleHandleW.restype = wt.HMODULE
        mon = self.monitor

        def mouse_proc(code, wparam, lparam):
            if code == 0:
                try:
                    s = ctypes.cast(lparam, ctypes.POINTER(MSLL)).contents
                    injected = bool(s.flags & 0x3)                 # LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
                    mon.on_mouse(int(wparam), s.pt.x, s.pt.y, injected, injected and s.dwExtraInfo == RUNNER_TAG)
                except Exception:
                    pass
            return u32.CallNextHookEx(None, code, wparam, lparam)

        def key_proc(code, wparam, lparam):
            if code == 0:
                try:
                    s = ctypes.cast(lparam, ctypes.POINTER(KBDLL)).contents
                    injected = bool(s.flags & 0x12)                # LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
                    mon.on_key(int(s.vkCode), int(wparam) in (WM_KEYDOWN, WM_SYSKEYDOWN), injected,
                               injected and s.dwExtraInfo == RUNNER_TAG)
                except Exception:
                    pass
            return u32.CallNextHookEx(None, code, wparam, lparam)

        self._procs = (HOOKPROC(mouse_proc), HOOKPROC(key_proc))   # keep references alive
        hmod = k32.GetModuleHandleW(None)
        hm = u32.SetWindowsHookExW(14, self._procs[0], hmod, 0)    # WH_MOUSE_LL
        hk = u32.SetWindowsHookExW(13, self._procs[1], hmod, 0)    # WH_KEYBOARD_LL
        self._tid = k32.GetCurrentThreadId()
        if not (hm and hk):
            self.error = 'SetWindowsHookEx failed (error %d)' % ctypes.get_last_error()
            for h in (hm, hk):
                if h:
                    u32.UnhookWindowsHookEx(h)
            self._ready.set()
            return
        self.ok = True
        self._ready.set()
        msg = wt.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        u32.UnhookWindowsHookEx(hm)
        u32.UnhookWindowsHookEx(hk)

    def stop(self):
        if self._tid:
            import ctypes
            ctypes.WinDLL('user32').PostThreadMessageW(self._tid, 0x0012, 0, 0)   # WM_QUIT
        if self._th:
            self._th.join(1.0)
