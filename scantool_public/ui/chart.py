"""Tiny live chart — QPainter only, no QtCharts / matplotlib."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

_COLORS = (
    QColor("#38bdf8"),
    QColor("#22c55e"),
    QColor("#f59e0b"),
    QColor("#c084fc"),
)
_MAX_POINTS = 240
_MAX_TRACES = 4


class LiveChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("liveChart")
        self.setMinimumHeight(128)
        self.setMaximumHeight(168)
        self._series: dict[str, list[tuple[float, float]]] = {}
        self._names: dict[str, str] = {}
        self._marks: list[float] = []
        self._t0: float | None = None
        self.setToolTip("Live values. Vertical ticks are marked events.")

    def reset(self, pids: list[str], names: dict[str, str] | None = None) -> None:
        self._series = {p.upper(): [] for p in pids[:_MAX_TRACES]}
        self._names = {k.upper(): (names or {}).get(k, k) for k in self._series}
        self._marks = []
        self._t0 = None
        self.update()

    def add_row(self, ts: float, values: dict[str, float | None]) -> None:
        if self._t0 is None:
            self._t0 = ts
        t = ts - self._t0
        for pid, series in self._series.items():
            v = values.get(pid)
            if v is None:
                continue
            series.append((t, float(v)))
            if len(series) > _MAX_POINTS:
                del series[: len(series) - _MAX_POINTS]
        self.update()

    def add_mark(self, ts: float) -> None:
        if self._t0 is None:
            self._t0 = ts
        self._marks.append(ts - self._t0)
        if len(self._marks) > 40:
            self._marks = self._marks[-40:]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect().adjusted(6, 6, -6, -6)
        p.fillRect(self.rect(), QColor("#141c2b"))
        p.setPen(QPen(QColor("#1e2a3c")))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)

        series = [(pid, pts) for pid, pts in self._series.items() if len(pts) >= 2]
        if not series:
            p.setPen(QColor("#64748b"))
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "Start a log to see the chart")
            p.end()
            return

        t_min = min(pts[0][0] for _, pts in series)
        t_max = max(pts[-1][0] for _, pts in series)
        if t_max <= t_min:
            t_max = t_min + 1.0
        pad = 22
        plot = r.adjusted(4, 4, -4, -pad)

        p.setPen(QPen(QColor("#1e2a3c"), 1))
        for i in range(1, 4):
            y = plot.top() + plot.height() * i / 4
            p.drawLine(plot.left(), int(y), plot.right(), int(y))

        p.setPen(QPen(QColor("#fdba74"), 1, Qt.PenStyle.DashLine))
        span = t_max - t_min
        for mt in self._marks:
            if mt < t_min or mt > t_max:
                continue
            x = plot.left() + (mt - t_min) / span * plot.width()
            p.drawLine(int(x), plot.top(), int(x), plot.bottom())

        legend_x = plot.left()
        font = QFont(p.font())
        font.setPointSize(8)
        p.setFont(font)
        for i, (pid, pts) in enumerate(series):
            color = _COLORS[i % len(_COLORS)]
            ys = [v for _, v in pts]
            lo, hi = min(ys), max(ys)
            if hi <= lo:
                hi = lo + 1.0
            path = QPainterPath()
            for j, (t, v) in enumerate(pts):
                x = plot.left() + (t - t_min) / span * plot.width()
                y = plot.bottom() - (v - lo) / (hi - lo) * plot.height()
                pt = QPointF(x, y)
                if j == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
            pen = QPen(color, 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPath(path)
            name = self._names.get(pid, pid)
            label = f"{pid} {name}"
            p.drawText(QRectF(legend_x, r.bottom() - 16, 160, 16), label)
            legend_x += 170
        p.end()
