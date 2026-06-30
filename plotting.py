"""Live plot subprocess (process-isolated to keep BLE loop responsive)."""

import multiprocessing as mp
import queue as queue_mod
import sys
import time
import traceback
from collections import deque
from typing import Any, List, Optional

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


# Module-level strong refs to FuncAnimation instances.
# Required because Matplotlib only weakly references them; without this,
# the animation is GC'd and the plot silently freezes.
_ANIMS: List[FuncAnimation] = []


def _plot_proc(queue: mp.Queue, title: str, ylabel: str,
               window_s: float, series: List[str]):
    """
        Matplotlib live plot — runs in subprocess.
        `series` is a list of per-line labels. If empty/None, a single line.
    """

    try:
        # Try backends in order; first one that loads wins.
        for backend in ("TkAgg", "Qt5Agg", "QtAgg"):
            try:
                matplotlib.use(backend, force=True)
                break

            except (ImportError, ValueError):
                continue

        n_series = max(1, len(series))
        labels = list(series) if series else [None]

        ts: deque = deque()
        ys_list: List[deque] = [deque() for _ in range(n_series)]

        fig, ax = plt.subplots()
        lines = []
        for i in range(n_series):
            ln, = ax.plot([], [], lw=1.5, label=labels[i])
            lines.append(ln)

        ax.set_title(title)
        ax.set_xlabel("time [s]")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)

        if series:
            ax.legend(loc="upper right")

        t0 = time.monotonic()

        def update(_: Any) -> Any:
            """
                Private callback that iterates the animation
            """
            # Drain queue
            while True:
                try:
                    item = queue.get_nowait()

                except queue_mod.Empty:
                    break

                if item is None:
                    plt.close(fig)
                    return tuple(lines)

                # Control messages: ("title", "...") etc.
                if (isinstance(item, tuple) and len(item) == 2
                        and isinstance(item[0], str)):
                    tag, payload = item
                    if tag == "title":
                        ax.set_title(str(payload))
                        fig.canvas.draw_idle()
                    continue

                # Data sample: (t_float, v)  where v is float or tuple
                t, v = item
                ts.append(t - t0)

                if isinstance(v, (tuple, list)):
                    for i in range(n_series):
                        ys_list[i].append(
                            float(v[i]) if i < len(v) else float("nan"))
                else:
                    ys_list[0].append(float(v))

            cutoff = (time.monotonic() - t0) - window_s

            while ts and ts[0] < cutoff:
                ts.popleft()
                for ys in ys_list:
                    if ys:
                        ys.popleft()

            if ts:
                for i, ln in enumerate(lines):
                    ln.set_data(ts, ys_list[i])
                ax.relim()
                ax.autoscale_view()

            return tuple(lines)

        # IMPORTANT: keep a strong reference to `ani`, else it gets GC'd
        # and the animation silently freezes.
        ani = FuncAnimation(
            fig, update, interval=100, blit=False, cache_frame_data=False
        )

        _ANIMS.append(ani)   # extra anchor against GC

        plt.show()

    except (RuntimeError, ImportError, OSError):
        # Print full traceback to the *parent* terminal so it's visible
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()


class LivePlot:
    """
        handle to a subprocess-based live plot.
    """

    def __init__(self, title: str, ylabel: str, window_s: float = 30.0,
                 series: Optional[List[str]] = None):
        # spawn is required on Windows; explicit for portability.
        ctx = mp.get_context("spawn")
        self.queue: mp.Queue = ctx.Queue()
        self.proc = ctx.Process(
            target=_plot_proc,
            args=(self.queue, title, ylabel, window_s, series or []),
            daemon=True,
        )
        self.proc.start()

    def push(self, value: Any):
        """
            Push a new sample. `value` can be:
                - a single float                  → 1 series
                - a tuple/list of floats (xyz...) → N series (one per element)
        """
        if not self.proc.is_alive():
            return  # plot subprocess died — drop sample silently
        try:
            self.queue.put_nowait((time.monotonic(), value))
        except queue_mod.Full:
            pass

    def set_title(self, title: str):
        """Update the plot title live (sent to the subprocess)."""
        if not self.proc.is_alive():
            return
        try:
            self.queue.put_nowait(("title", title))
        except queue_mod.Full:
            pass

    def close(self) -> Any:
        """
            Callback on closing the Plot
        """

        try:
            self.queue.put_nowait(None)
        except queue_mod.Full:
            pass

        if self.proc.is_alive():
            self.proc.join(timeout=2.0)
            if self.proc.is_alive():
                self.proc.terminate()


def _bar_plot_proc(queue: mp.Queue, title: str, xlabel: str, ylabel: str):
    """
        Matplotlib live bar chart — runs in subprocess.
        Each frame replaces the bars; no history accumulation.
    """

    try:
        for backend in ("TkAgg", "Qt5Agg", "QtAgg"):
            try:
                matplotlib.use(backend, force=True)
                break
            except (ImportError, ValueError):
                continue

        fig, ax = plt.subplots()

        # Mutable holders so control messages survive ax.clear() redraws
        current_title = [title]

        ax.set_title(current_title[0])
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)

        cur_x: List[float] = []
        cur_y: List[float] = []

        def update(_: Any) -> Any:
            """
                Drain queue, keep the latest bar data, redraw.
            """
            nonlocal cur_x, cur_y
            latest = None

            while True:
                try:
                    item = queue.get_nowait()
                except queue_mod.Empty:
                    break

                if item is None:
                    plt.close(fig)
                    return ()

                # Control messages: ("title", "...")
                if (isinstance(item, tuple) and len(item) == 2
                        and isinstance(item[0], str)):
                    if item[0] == "title":
                        current_title[0] = str(item[1])
                        ax.set_title(current_title[0])
                        fig.canvas.draw_idle()
                    continue

                # Bar data: (x_list, y_list)
                latest = item

            if latest is not None:
                cur_x, cur_y = list(latest[0]), list(latest[1])

                ax.clear()
                ax.set_title(current_title[0]) 
                ax.set_xlabel(xlabel)
                ax.set_ylabel(ylabel)
                ax.grid(True, alpha=0.3)

                if cur_x:
                    span = max(cur_x) - min(cur_x) if len(cur_x) > 1 else 1.0
                    width = max(0.1, 0.9 * span / max(1, len(cur_x)))
                    ax.bar(cur_x, cur_y, width=width)

            return ()

        ani = FuncAnimation(
            fig, update, interval=200, blit=False, cache_frame_data=False
        )
        _ANIMS.append(ani)

        plt.show()

    except (RuntimeError, ImportError, OSError):
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()


class LiveBarPlot:
    """
        Handle to a subprocess-based live bar chart.
    """

    def __init__(self, title: str, xlabel: str, ylabel: str):
        # spawn is required on Windows; explicit for portability.
        ctx = mp.get_context("spawn")
        self.queue: mp.Queue = ctx.Queue()
        self.proc = ctx.Process(
            target=_bar_plot_proc,
            args=(self.queue, title, xlabel, ylabel),
            daemon=True,
        )
        self.proc.start()

    def push(self, x_array, y_array) -> Any:
        """
            Replace the current bars with a new (x, y) frame.
        """

        if not self.proc.is_alive():
            return
        try:
            self.queue.put_nowait((list(x_array), list(y_array)))
        except queue_mod.Full:
            pass

    def set_title(self, title: str):
        """
            Update the plot title live.
        """

        if not self.proc.is_alive():
            return
        try:
            self.queue.put_nowait(("title", title))
        except queue_mod.Full:
            pass

    def close(self) -> Any:
        """
            Callback function on closing the FFT
        """

        try:
            self.queue.put_nowait(None)
        except queue_mod.Full:
            pass

        if self.proc.is_alive():
            self.proc.join(timeout=2.0)
            if self.proc.is_alive():
                self.proc.terminate()
