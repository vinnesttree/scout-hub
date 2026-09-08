from app.services.ocr import _candidate_tokens, _vote_score, _vote_teams


def test_vrc_numeric_codes():
    assert _candidate_tokens("1234A 5678B") == ["1234A", "5678B"]
    assert _candidate_tokens("62309X") == ["62309X"]


def test_vexu_alpha_codes():
    toks = _candidate_tokens("BLRS1 BLRS2")
    assert toks == ["BLRS1", "BLRS2"]
    assert "GHST" in _candidate_tokens("GHST QCC2")


def test_digit_confusions_fixed_in_numeric_part():
    # tesseract often reads 1 as I/L and 0 as O
    assert _candidate_tokens("I234A") == ["1234A"]
    assert _candidate_tokens("O0O5Z") == ["0005Z"]


def test_stopwords_filtered():
    assert _candidate_tokens("RED ALLIANCE  WINNER") == []
    assert _candidate_tokens("AUTO DRIVER FINAL") == []


def test_vote_teams_takes_majority_across_frames():
    samples = ["BLRS1 BLRS2", "BLR51 BLRS2", "BLRS1 BLRS2", "garbage"]
    assert set(_vote_teams(samples)[:2]) == {"BLRS1", "BLRS2"}


def test_vote_score():
    assert _vote_score(["88", "8", "88", "88"]) == 88
    assert _vote_score(["", "x"]) is None
