"""Input drivers and the safety guard.

WinInputDriver uses ONLY the operating system's input queue (user32 SendInput), exactly like a physical
keyboard/mouse. It never opens the game process for writing, injects code or loads anything into the game.

Safety (all paths release every held key/button):
  * inputs are sent only while the game window is the foreground window; focus loss aborts the run;
  * emergency stop: F12 or Pause/Break (polled every loop iteration; the runner never sends these keys);
  * user takeover: any PHYSICAL mouse/keyboard input (or another program's key/button) while the runner owns
    the game aborts. Runner input is told apart by a tag on every SendInput event, seen through low-level
    hooks (inputhook.py); the game moving the cursor itself is never a takeover. Fallback when hooks cannot
    be installed: the old cursor-position comparison, with a warning;
  * watchdog thread: if the main loop stops polling for WATCHDOG_S while inputs are held, it releases them;
  * Ctrl+C in the console, any exception, and interpreter exit (atexit) release everything.
"""
import atexit
import threading
import time

from .inputhook import RUNNER_TAG, LowLevelHooks, TakeoverMonitor

WATCHDOG_S = 0.75
MOUSE_TAKEOVER_PX = 48


class AbortRun(Exception):
    """Stop the whole run immediately (emergency stop, focus loss, user takeover, watchdog)."""


class InputDriver:
    """Interface shared by WinInputDriver and mock.MockInputDriver."""
    def __init__(self):
        self.held = set()
        self.mouse = None              # last client position set by the runner

    def press(self, key):
        raise NotImplementedError

    def release(self, key):
        raise NotImplementedError

    def move_mouse(self, x, y):
        raise NotImplementedError

    def client_size(self):
        raise NotImplementedError

    def release_all(self):
        for k in list(self.held):
            try:
                self.release(k)
            except Exception:       # never let a failed key-up hide the others
                pass

    # safety hooks
    def foreground_ok(self):
        return True

    def emergency_pressed(self):
        return False

    def takeover(self):
        """Reason string when the user took over since arm(), else None."""
        return None

    def arm(self):
        """Start takeover detection (the runner owns the game from now on)."""

    def disarm(self):
        """Stop takeover detection (console prompt, manual setup)."""

    def cursor_client(self):
        """Actual cursor position in game-window client coordinates (diagnostics), or None."""
        return None

    def cursor_state(self):
        """Diagnostics: cursor visibility / clipping as Windows reports them."""
        return {}

    def move_mouse_relative(self, dx, dy):
        raise NotImplementedError

    def focus_game(self, timeout_s=15.0):
        return True


class Watchdog:
    """Releases all inputs if the runner's loop stops beating while inputs are held."""
    def __init__(self, driver, limit_s=WATCHDOG_S, clock=time.monotonic):
        self.driver, self.limit, self.clock = driver, limit_s, clock
        self.beat_t = clock()
        self.tripped = None
        self._stop = threading.Event()
        self._th = None

    def beat(self):
        self.beat_t = self.clock()

    def check(self):
        """One watchdog evaluation (called by the thread; callable directly in tests)."""
        if self.driver.held and self.clock() - self.beat_t > self.limit:
            self.driver.release_all()
            self.tripped = 'watchdog: runner loop stalled %.2f s with inputs held; all inputs released' % (
                self.clock() - self.beat_t)
        return self.tripped

    def start(self):
        def loop():
            while not self._stop.wait(0.05):
                self.check()
        self._th = threading.Thread(target=loop, name='input-watchdog', daemon=True)
        self._th.start()
        return self

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(1.0)


class SafetyGuard:
    """Checked once per loop iteration by the runner; raises AbortRun with the reason."""
    def __init__(self, driver, watchdog=None, mouse_failsafe=True):
        self.driver, self.watchdog, self.mouse_failsafe = driver, watchdog, mouse_failsafe
        self.require_focus = True        # False only for observe-only use (-Check): no input is ever sent then
        self.reason = None
        atexit.register(driver.release_all)

    def check(self):
        if self.watchdog:
            self.watchdog.beat()
            if self.watchdog.tripped:
                self.abort(self.watchdog.tripped)
        if self.driver.emergency_pressed():
            self.abort('EMERGENCY STOP (F12 / Pause pressed)')
        if self.require_focus and not self.driver.foreground_ok():
            self.abort('the game window lost focus (no inputs are sent to other windows)')
        if self.mouse_failsafe:
            r = self.driver.takeover()
            if r:
                self.abort('user takeover: %s' % r)

    def abort(self, reason):
        self.driver.release_all()
        self.reason = reason
        raise AbortRun(reason)


# ------------------------------------------------------------------------------------------ Windows
class WinInputDriver(InputDriver):
    """SendInput-based driver. Windows only; imported lazily by the live runner."""
    VK = {'SPACE': 0x20, 'LEFT': 0x25, 'UP': 0x26, 'RIGHT': 0x27, 'DOWN': 0x28, 'SHIFT': 0x10, 'CTRL': 0x11,
          'ALT': 0x12, 'ENTER': 0x0D, 'TAB': 0x09}
    EXTENDED = {'LEFT', 'UP', 'RIGHT', 'DOWN'}
    MOUSE = {'LMB': (0x0002, 0x0004), 'RMB': (0x0008, 0x0010), 'MMB': (0x0020, 0x0040)}
    EMERGENCY_VK = (0x7B, 0x13)       # F12, Pause
    FORBIDDEN = {'F12', 'PAUSE', 'F9', 'F10', 'ESC', 'ESCAPE'}   # stop key, probe hotkeys, game menu

    def __init__(self, pid):
        super().__init__()
        import ctypes
        import ctypes.wintypes as wt
        self.ct, self.wt = ctypes, wt
        self.u32 = ctypes.WinDLL('user32', use_last_error=True)
        try:                                       # physical pixels for window rects and cursor positions
            ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.u32.SetProcessDPIAware()
            except Exception:
                pass
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                        ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                        ('dwExtraInfo', ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [('uMsg', wt.DWORD), ('wParamL', wt.WORD), ('wParamH', wt.WORD)]

        class _U(ctypes.Union):
            _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [('type', wt.DWORD), ('u', _U)]

        self.INPUT = INPUT
        self.u32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.u32.SendInput.restype = wt.UINT
        self.u32.GetForegroundWindow.restype = wt.HWND
        self.u32.MapVirtualKeyW.restype = wt.UINT
        self.u32.GetAsyncKeyState.restype = ctypes.c_short
        self.pid = pid
        self.hwnd = self._find_window(pid)
        if not self.hwnd:
            raise RuntimeError('no visible window belongs to webbed.exe (pid %d)' % pid)
        self._last_move_t = 0.0
        self.monitor = TakeoverMonitor()
        self.hooks = LowLevelHooks(self.monitor)
        self.hook_ok = self.hooks.start()
        if not self.hook_ok:
            print('WARNING: low-level input hooks unavailable (%s). Takeover detection falls back to comparing the '
                  'cursor position, which false-triggers if the game moves the cursor. F12 / Pause and focus-loss '
                  'protection are unaffected.' % self.hooks.error)
        atexit.register(self.hooks.stop)

    # ---- window
    def _find_window(self, pid):
        ct, wt, u32 = self.ct, self.wt, self.u32
        found = []
        proto = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(hwnd, _):
            p = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ct.byref(p))
            if p.value == pid and u32.IsWindowVisible(hwnd) and u32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
            return True
        u32.EnumWindows(proto(cb), 0)
        return found[0] if found else None

    def _client_origin_size(self):
        ct, wt = self.ct, self.wt
        r = wt.RECT()
        self.u32.GetClientRect(self.hwnd, ct.byref(r))
        pt = wt.POINT(0, 0)
        self.u32.ClientToScreen(self.hwnd, ct.byref(pt))
        return (pt.x, pt.y), (r.right - r.left, r.bottom - r.top)

    def client_size(self):
        return self._client_origin_size()[1]

    def foreground_ok(self):
        return self.u32.GetForegroundWindow() == self.hwnd

    FOCUS_STABLE_S = 0.3

    def focus_game(self, timeout_s=15.0):
        """Bring the game to the foreground WITHOUT injecting any input into another window.
        1. restore it if minimised; 2. SetForegroundWindow; 3. if Windows' foreground lock refuses, attach this
        thread's input queue to the current foreground thread (AttachThreadInput), retry, detach; 4. otherwise ask
        the user to click the game's TITLE BAR (a click inside the game would shoot a web).
        Nothing may be held (runner releases first); the emergency stop is polled throughout and aborts; the
        game must then stay in the foreground for FOCUS_STABLE_S before the runner may send anything."""
        u32, k32 = self.u32, self.ct.WinDLL('kernel32')
        if self.held:
            self.release_all()
        if u32.IsIconic(self.hwnd):
            u32.ShowWindow(self.hwnd, 9)                       # SW_RESTORE
        if not self.foreground_ok():
            u32.SetForegroundWindow(self.hwnd)
        if not self.foreground_ok():
            fg = u32.GetForegroundWindow()
            fg_tid = u32.GetWindowThreadProcessId(fg, None) if fg else 0
            me = k32.GetCurrentThreadId()
            attached = bool(fg_tid and fg_tid != me and u32.AttachThreadInput(me, fg_tid, True))
            try:
                u32.BringWindowToTop(self.hwnd)
                u32.SetForegroundWindow(self.hwnd)
            finally:
                if attached:
                    u32.AttachThreadInput(me, fg_tid, False)
        t_end = time.monotonic() + timeout_s
        asked, stable_since = False, None
        while time.monotonic() < t_end:
            if self.emergency_pressed():
                raise AbortRun('EMERGENCY STOP (F12 / Pause pressed) while waiting for the game window')
            if self.foreground_ok():
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= self.FOCUS_STABLE_S:
                    self.mouse = None                          # takeover baseline restarts after a focus change
                    return True
            else:
                stable_since = None
                if not asked:
                    print('  Click the Webbed window\'s TITLE BAR to give it focus (not inside the game: that would '
                          'shoot a web). Waiting %d s; F12 / Pause aborts.' % timeout_s)
                    asked = True
            time.sleep(0.02)
        return False

    # ---- safety
    def emergency_pressed(self):
        return self.monitor.emergency or any(self.u32.GetAsyncKeyState(vk) & 0x8000 for vk in self.EMERGENCY_VK)

    def arm(self):
        self.mouse = None
        self.monitor.arm()

    def disarm(self):
        self.monitor.disarm()

    def takeover(self):
        if self.hook_ok:
            return self.monitor.takeover()
        # fallback (no hooks): cursor far from where the runner put it
        if not self.monitor.armed or self.mouse is None or time.monotonic() - self._last_move_t < 0.1:
            return None
        c = self.cursor_client()
        if c and abs(c[0] - self.mouse[0]) + abs(c[1] - self.mouse[1]) > MOUSE_TAKEOVER_PX:
            return 'cursor is %s, runner put it at %s (position fallback; hooks unavailable)' % (c, self.mouse)
        return None

    def cursor_client(self):
        ct, wt = self.ct, self.wt
        pt = wt.POINT()
        self.u32.GetCursorPos(ct.byref(pt))
        (ox, oy), _ = self._client_origin_size()
        return (pt.x - ox, pt.y - oy)

    def cursor_state(self):
        ct, wt = self.ct, self.wt

        class CURSORINFO(ct.Structure):
            _fields_ = [('cbSize', wt.DWORD), ('flags', wt.DWORD), ('hCursor', wt.HANDLE), ('pt', wt.POINT)]
        ci = CURSORINFO()
        ci.cbSize = ct.sizeof(ci)
        self.u32.GetCursorInfo(ct.byref(ci))
        clip = wt.RECT()
        self.u32.GetClipCursor(ct.byref(clip))
        (ox, oy), (w, h) = self._client_origin_size()
        return {'visible': bool(ci.flags & 1), 'clip_screen': [clip.left, clip.top, clip.right, clip.bottom],
                'client_origin_screen': [ox, oy], 'client_size': [w, h]}

    # ---- input
    def _send(self, inp):
        n = self.u32.SendInput(1, self.ct.byref(inp), self.ct.sizeof(self.INPUT))
        if n != 1:
            raise AbortRun('SendInput was blocked (error %d); is the console running elevated differently from the game?'
                           % self.ct.get_last_error())

    def _vk(self, key):
        k = key.upper()
        if k in self.FORBIDDEN:
            raise ValueError('key %s is reserved (emergency stop / probe hotkeys / menu)' % key)
        if k in self.VK:
            return self.VK[k]
        if len(k) == 1 and (k.isalpha() or k.isdigit()):
            return ord(k)
        raise ValueError('unknown key name %r' % key)

    def _key_event(self, key, up):
        vk = self._vk(key)
        scan = self.u32.MapVirtualKeyW(vk, 0)
        flags = 0x0008 | (0x0002 if up else 0)                 # KEYEVENTF_SCANCODE | KEYUP
        if key.upper() in self.EXTENDED:
            flags |= 0x0001                                    # KEYEVENTF_EXTENDEDKEY
        inp = self.INPUT(type=1)
        inp.u.ki = type(inp.u.ki)(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=RUNNER_TAG)
        self._send(inp)

    def _mouse_event(self, flags, dx=0, dy=0):
        inp = self.INPUT(type=0)
        inp.u.mi = type(inp.u.mi)(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=RUNNER_TAG)
        self._send(inp)

    def press(self, key):
        if not self.foreground_ok():
            raise AbortRun('refusing to send %s: the game window is not in the foreground' % key)
        k = key.upper()
        if k in self.MOUSE:
            self._mouse_event(self.MOUSE[k][0])
        else:
            self._key_event(k, up=False)
        self.held.add(k)

    def release(self, key):
        k = key.upper()
        if k in self.MOUSE:
            self._mouse_event(self.MOUSE[k][1])
        else:
            self._key_event(k, up=True)
        self.held.discard(k)

    def move_mouse(self, x, y):
        """Move the cursor to client coordinates (x, y) of the game window (absolute, virtual desktop)."""
        if not self.foreground_ok():
            raise AbortRun('refusing to move the mouse: the game window is not in the foreground')
        (ox, oy), (w, h) = self._client_origin_size()
        x, y = int(max(2, min(w - 3, x))), int(max(2, min(h - 3, y)))
        gsm = self.u32.GetSystemMetrics
        vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)
        nx = int(round((ox + x - vx) * 65535 / max(vw - 1, 1)))
        ny = int(round((oy + y - vy) * 65535 / max(vh - 1, 1)))
        self._mouse_event(0x0001 | 0x8000 | 0x4000, nx, ny)     # MOVE | ABSOLUTE | VIRTUALDESK
        self.mouse = (x, y)
        self._last_move_t = time.monotonic()

    def move_mouse_relative(self, dx, dy):
        """Relative mouse motion (mickeys; subject to pointer acceleration) for games that read deltas."""
        if not self.foreground_ok():
            raise AbortRun('refusing to move the mouse: the game window is not in the foreground')
        self._mouse_event(0x0001, int(dx), int(dy))
        self._last_move_t = time.monotonic()
