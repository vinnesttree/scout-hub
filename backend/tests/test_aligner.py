from app.services.aligner import align
from app.services.match_detect import MatchWindow
from app.services.robotevents import REAlliance, REMatch, RETeamRef


def _match(i):
    return REMatch(
        re_match_id=i,
        name=f"Qualification {i}",
        round="qual",
        match_num=i,
        instance=1,
        scheduled_at=None,
        started_at=None,
        division_id=1,
        alliances=[
            REAlliance("red", 10 + i, [RETeamRef(f"{i}A"), RETeamRef(f"{i}B")]),
            REAlliance("blue", 5 + i, [RETeamRef(f"{i}C"), RETeamRef(f"{i}D")]),
        ],
    )


def _win(start):
    return MatchWindow(start_s=start, end_s=start + 110, confidence=0.9, n_reads=100)


def test_exact_alignment():
    schedule = [_match(i) for i in range(1, 6)]
    windows = [_win(200 + i * 300) for i in range(5)]
    res = align(windows, schedule)
    assert len(res.matched) == 5
    assert not res.unmatched_windows
    for pair, w in zip(res.pairs, windows):
        assert pair.window is w


def test_extra_window_is_unmatched():
    schedule = [_match(i) for i in range(1, 4)]
    windows = [_win(200 + i * 300) for i in range(4)]
    res = align(windows, schedule)
    assert len(res.matched) == 3
    assert len(res.unmatched_windows) == 1
    assert res.unmatched_windows[0].start_s == windows[3].start_s


def test_missing_window_leaves_scheduled_match_unpaired():
    schedule = [_match(i) for i in range(1, 5)]
    # only 3 windows, evenly spaced, so #4 has nothing
    windows = [_win(200), _win(500), _win(800)]
    res = align(windows, schedule)
    assert len(res.pairs) == 4
    assert res.pairs[3].window is None
    assert not res.unmatched_windows
