"""Matplotlib canvas shared by the tilt page and the path editor."""

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure


class PointsCanvas(FigureCanvasQTAgg):
    """XY point-path plot with per-point status colours and a hover readout."""

    COLORS = {"ok": "#1b8756", "failed": "#bf3a2b", "stopped": "#a96300",
              "pending": "#ffffff", "active": "#1f5fbf"}

    def __init__(self):
        self.fig = Figure(figsize=(5, 3.2), dpi=100, constrained_layout=True)
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self.points = ()          # tuple[(x_m, y_m)]
        self.status = {}          # index -> ok/failed/stopped
        self.active = None
        self.current_xy = None
        self.annotation = self.ax.annotate(
            "", xy=(0, 0), xytext=(10, 12), textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.35", fc="#18222f", ec="none", alpha=0.9),
            color="#ffffff", fontsize=8, visible=False)
        self.mpl_connect("motion_notify_event", self._hover)
        self.redraw()

    def set_data(self, points, status=None, active=None, current_xy=None):
        self.points = tuple(points)
        self.status = dict(status or {})
        self.active = active
        self.current_xy = current_xy
        self.redraw()

    def redraw(self):
        ax = self.ax
        ax.clear()
        self.annotation = ax.annotate(
            "", xy=(0, 0), xytext=(10, 12), textcoords="offset points",
            bbox=dict(boxstyle="round,pad=0.35", fc="#18222f", ec="none", alpha=0.9),
            color="#ffffff", fontsize=8, visible=False)
        ax.set_facecolor("#fbfcfd")
        for spine in ax.spines.values():
            spine.set_color("#dde3ea")
        ax.tick_params(colors="#8a95a3", labelsize=8)
        ax.grid(True, color="#edf0f4", linewidth=1)
        ax.set_xlabel("X (µm)", color="#8a95a3", fontsize=8)
        ax.set_ylabel("Y (µm)", color="#8a95a3", fontsize=8)
        pts = [(x * 1e6, y * 1e6) for x, y in self.points]
        if len(pts) > 1:
            xs, ys = zip(*pts)
            ax.plot(xs, ys, linestyle="--", color="#b9c4d0", linewidth=1.2, zorder=1)
        for index, (x, y) in enumerate(pts, 1):
            state = self.status.get(index, "pending")
            if index == self.active:
                ax.scatter([x], [y], s=150, facecolor="none", edgecolor="#1f5fbf",
                           linewidth=1.6, zorder=3)
                color = self.COLORS["active"]
            else:
                color = self.COLORS.get(state, "#ffffff")
            ax.scatter([x], [y], s=70, facecolor=color, edgecolor="#5b6672",
                       linewidth=1.2, zorder=4)
            ax.annotate(str(index), (x, y), ha="center", va="center", fontsize=6.5,
                        color="#ffffff" if state in self.COLORS and state != "pending" else "#5b6672",
                        zorder=5)
        if self.current_xy:
            cx, cy = self.current_xy[0] * 1e6, self.current_xy[1] * 1e6
            ax.scatter([cx], [cy], marker="+", s=140, color="#bf3a2b", linewidth=1.6, zorder=6)
        if not pts:
            ax.text(.5, .5, "加载配方或编辑点位后显示路径", transform=ax.transAxes,
                    ha="center", va="center", color="#8a95a3", fontsize=9)
        self.draw_idle()

    def _hover(self, event):
        if event.inaxes is not self.ax or not self.points:
            self.annotation.set_visible(False)
            self.draw_idle()
            return
        best, dist = None, None
        for index, (x, y) in enumerate(self.points, 1):
            dx, dy = event.xdata - x * 1e6, event.ydata - y * 1e6
            d = dx * dx + dy * dy
            if dist is None or d < dist:
                best, dist = index, d
        limit = (self.ax.get_xlim()[1] - self.ax.get_xlim()[0]) * .03
        if dist is not None and dist ** .5 <= limit:
            x, y = self.points[best - 1]
            self.annotation.xy = (x * 1e6, y * 1e6)
            state = {"ok": "完成", "failed": "失败", "stopped": "中止"}.get(
                self.status.get(best), "当前" if best == self.active else "待测")
            self.annotation.set_text(
                f"点 {best} · {state}\nX {x * 1e6:.3f} µm   Y {y * 1e6:.3f} µm")
            self.annotation.set_visible(True)
        else:
            self.annotation.set_visible(False)
        self.draw_idle()
