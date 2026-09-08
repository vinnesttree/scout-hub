from app.services.match_detect import (
    TimerSample,
    assemble_windows,
    assemble_windows_from_labels,
)


def _timeline(spec, step=1.0):
    """spec: list of (n_frames, seconds_or_None) -> flat TimerSample list."""
    out, t = [], 0.0
    for n, val in spec:
        for _ in range(n):
            out.append(TimerSample(t, val))
            t += step
    return out


def test_single_match_window():
    # 10s pre-show blank, 100s of a counting-down timer, 20s blank
    samples = _timeline([(10, None)])
    t = 10.0
    for s in range(105, 0, -1):
        samples.append(TimerSample(t, s))
        t += 1.0
    samples += [TimerSample(t + i, None) for i in range(20)]

    windows = assemble_windows(samples)
    assert len(windows) == 1
    w = windows[0]
    assert 9 <= w.start_s <= 11
    assert 113 <= w.end_s <= 116
    assert w.confidence > 0.7


def test_two_matches_separated_by_gap():
    samples = []
    t = 0.0
    for _ in range(2):
        for s in range(90, 0, -1):
            samples.append(TimerSample(t, s))
            t += 1.0
        # 60s gap with no timer
        for _ in range(60):
            samples.append(TimerSample(t, None))
            t += 1.0
    windows = assemble_windows(samples)
    assert len(windows) == 2
    assert windows[0].end_s < windows[1].start_s


def test_short_blips_are_ignored():
    samples = _timeline([(5, None), (3, 100), (5, None), (2, 95), (5, None)])
    assert assemble_windows(samples) == []


def _lbl(t, present, num=None):
    return TimerSample(t, None, overlay=present, label_num=num)


def test_label_windows_static_timer_stream():
    # overlay up for match 13 (0-110s), gap, up again for match 14 (150-260s)
    samples = []
    for t in range(0, 111, 2):
        samples.append(_lbl(t, True, 13))
    for t in range(112, 150, 2):
        samples.append(_lbl(t, False))
    for t in range(150, 261, 2):
        samples.append(_lbl(t, True, 14))
    windows = assemble_windows_from_labels(samples)
    assert len(windows) == 2
    assert windows[0].start_s < 5 and 105 <= windows[0].end_s <= 112
    assert windows[1].start_s >= 148


def test_label_windows_split_on_number_change_without_gap():
    # overlay never drops but the match number ticks 5 -> 6 partway through
    samples = []
    for t in range(0, 100, 2):
        samples.append(_lbl(t, True, 5))
    for t in range(100, 200, 2):
        samples.append(_lbl(t, True, 6))
    windows = assemble_windows_from_labels(samples)
    assert len(windows) == 2
    assert abs(windows[0].end_s - 98) <= 4
    assert abs(windows[1].start_s - 100) <= 4


def test_dropout_is_bridged():
    samples = []
    t = 0.0
    for s in range(120, 60, -1):        # 60s
        samples.append(TimerSample(t, s)); t += 1.0
    for _ in range(6):                   # 6s OCR dropout (< MAX_GAP_S)
        samples.append(TimerSample(t, None)); t += 1.0
    for s in range(60, 0, -1):           # 60s
        samples.append(TimerSample(t, s)); t += 1.0
    windows = assemble_windows(samples)
    assert len(windows) == 1
    assert windows[0].duration > 110
