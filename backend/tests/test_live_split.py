"""Incremental window assembly used while a capture is still running."""

from app.services.live_split import WindowTracker, match_name
from app.services.match_detect import MatchWindow, TimerSample


def _match(t0, length=100, step=2.0, label=None):
    out, t = [], float(t0)
    for i in range(int(length / step)):
        out.append(TimerSample(t, 120 - int(i * step), overlay=label is not None,
                               label_num=label, label_round="qual" if label else None))
        t += step
    return out


def _blank(t0, length, step=2.0):
    return [TimerSample(t0 + i * step, None) for i in range(int(length / step))]


def test_window_is_only_released_once_settled():
    tr = WindowTracker(step_s=2.0)
    tr.add(_blank(0, 10) + _match(10, 100))        # match 10-108
    assert tr.ready(110) == []                     # could still continue
    tr.add(_blank(110, 20))
    assert tr.ready(130) == []                     # 22 s after: still within the settle time
    tr.add(_blank(130, 20))
    (w,) = tr.ready(150)
    assert 9 <= w.start_s <= 11 and 106 <= w.end_s <= 110
    assert tr.ready(150) == []                     # handed out once only


def test_final_flush_releases_the_open_window():
    tr = WindowTracker(step_s=2.0)
    tr.add(_blank(0, 10) + _match(10, 100))
    assert tr.ready(110) == []
    assert len(tr.ready(110, final=True)) == 1


def test_back_to_back_matches_come_out_in_order():
    tr = WindowTracker(step_s=2.0)
    released = []
    t = 0.0
    for _ in range(3):
        tr.add(_blank(t, 40))
        t += 40
        tr.add(_match(t, 100))
        t += 100
        released += tr.ready(t)
    tr.add(_blank(t, 40))
    released += tr.ready(t + 40)
    assert len(released) == 3
    assert [round(w.start_s) for w in released] == [40, 180, 320]


def test_label_number_and_round_are_attached():
    tr = WindowTracker(step_s=2.0)
    tr.add(_blank(0, 10) + _match(10, 100, label=13) + _blank(110, 60))
    (w,) = tr.ready(170)
    assert (w.label_num, w.label_round) == (13, "qual")
    assert match_name(w, 1) == "Qualification 13"


def test_name_falls_back_to_ordinal():
    assert match_name(MatchWindow(0, 100, 0.9), 4) == "Match 4"
