"""Map detected video windows onto a RobotEvents match schedule.

The schedule is authoritative for *which* matches exist, their teams and their
scores. Detection gives us where each match sits in the recording. Both lists are
in chronological order, so this is a monotonic alignment: we walk the schedule and
the windows together, tolerating a leading offset (pre-show, an already-played
match not captured) and occasional missed detections.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.match_detect import MatchWindow
from app.services.robotevents import REMatch


@dataclass
class Alignment:
    re_match: REMatch
    window: MatchWindow | None            # None => scheduled match not found in video
    confidence: float = 0.0


@dataclass
class AlignResult:
    pairs: list[Alignment]
    unmatched_windows: list[MatchWindow]

    @property
    def matched(self) -> list[Alignment]:
        return [p for p in self.pairs if p.window is not None]


def align(
    windows: list[MatchWindow],
    schedule: list[REMatch],
    *,
    max_skew_ratio: float = 0.6,
) -> AlignResult:
    """Greedy monotonic alignment.

    ``windows`` and ``schedule`` must both be chronologically sorted. Windows that
    can't be tied to a scheduled match come back in ``unmatched_windows`` for
    manual review (e.g. an event not fully in the API, or a false positive).
    """
    windows = sorted(windows, key=lambda w: w.start_s)
    used_windows: set[int] = set()
    pairs: list[Alignment] = []

    wi = 0
    typical = _typical_spacing(windows)

    for sched in schedule:
        if wi >= len(windows):
            pairs.append(Alignment(sched, None, 0.0))
            continue
        win = windows[wi]
        # Accept the next window as this match unless it's implausibly far from the
        # previous accepted window (suggests a detection was missed).
        prev_end = pairs[-1].window.end_s if (pairs and pairs[-1].window) else None
        gap_ok = True
        if prev_end is not None and typical:
            gap = win.start_s - prev_end
            gap_ok = gap <= typical * (2.5 + max_skew_ratio * 4)
        if gap_ok:
            conf = round(0.5 + 0.5 * win.confidence, 3)
            pairs.append(Alignment(sched, win, conf))
            used_windows.add(wi)
            wi += 1
        else:
            # scheduled match with no detected window here
            pairs.append(Alignment(sched, None, 0.0))

    unmatched = [w for i, w in enumerate(windows) if i not in used_windows]
    return AlignResult(pairs=pairs, unmatched_windows=unmatched)


def _typical_spacing(windows: list[MatchWindow]) -> float | None:
    if len(windows) < 2:
        return None
    gaps = sorted(
        b.start_s - a.start_s for a, b in zip(windows, windows[1:]) if b.start_s > a.start_s
    )
    if not gaps:
        return None
    return gaps[len(gaps) // 2]  # median
