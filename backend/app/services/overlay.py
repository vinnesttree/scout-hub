"""Broadcast-overlay layout profiles.

A profile is a set of normalised ``[x0, y0, x1, y1]`` rectangles (0..1) describing
where the match timer, team numbers and scores sit on the broadcast overlay.
Layouts differ between events/seasons, so the Settings/calibration UI can add more.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "overlay_profiles"


Rect = tuple[float, float, float, float]


@dataclass
class OverlayProfile:
    name: str
    description: str
    timer_region: Rect
    red_teams_region: Rect
    blue_teams_region: Rect
    red_score_region: Rect
    blue_score_region: Rect
    # Round word + match number ("QUALIFICATION 13"). Used to detect a match even
    # when the broadcast has no live countdown (e.g. a static Tournament Manager
    # overlay), and to split back-to-back matches on the number changing.
    match_label_region: Rect = (0.20, 0.00, 0.40, 0.13)
    timer_invert: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayProfile":
        def rect(key: str, default: Rect) -> Rect:
            v = d.get(key, default)
            return (float(v[0]), float(v[1]), float(v[2]), float(v[3]))

        return cls(
            name=d.get("name", "custom"),
            description=d.get("description", ""),
            timer_region=rect("timer_region", (0.42, 0.01, 0.58, 0.13)),
            red_teams_region=rect("red_teams_region", (0.06, 0.80, 0.34, 0.97)),
            blue_teams_region=rect("blue_teams_region", (0.66, 0.80, 0.94, 0.97)),
            red_score_region=rect("red_score_region", (0.35, 0.80, 0.45, 0.97)),
            blue_score_region=rect("blue_score_region", (0.55, 0.80, 0.65, 0.97)),
            match_label_region=rect("match_label_region", (0.20, 0.00, 0.40, 0.13)),
            timer_invert=bool(d.get("timer_invert", False)),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "timer_region": list(self.timer_region),
            "red_teams_region": list(self.red_teams_region),
            "blue_teams_region": list(self.blue_teams_region),
            "red_score_region": list(self.red_score_region),
            "blue_score_region": list(self.blue_score_region),
            "match_label_region": list(self.match_label_region),
            "timer_invert": self.timer_invert,
        }


def _search_dirs() -> list[Path]:
    from app.config import get_settings

    custom = get_settings().data_dir / "overlay_profiles"
    return [custom, BUILTIN_DIR]


def list_profiles() -> list[str]:
    names: list[str] = []
    for d in _search_dirs():
        if d.is_dir():
            names.extend(p.stem for p in d.glob("*.json"))
    # de-dup, keep order (custom shadows builtin)
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out or ["vex_default"]


def load_profile(name: str) -> OverlayProfile:
    for d in _search_dirs():
        path = d / f"{name}.json"
        if path.is_file():
            return OverlayProfile.from_dict(json.loads(path.read_text()))
    if name != "vex_default":
        return load_profile("vex_default")
    # last-resort default if the builtin file is missing
    return OverlayProfile.from_dict({"name": "vex_default"})


def save_profile(profile: OverlayProfile) -> Path:
    from app.config import get_settings

    d = get_settings().data_dir / "overlay_profiles"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{profile.name}.json"
    path.write_text(json.dumps(profile.to_dict(), indent=2))
    return path


def crop_norm(img, rect: Rect):
    """Crop a normalised rect out of an OpenCV/np image (H, W, ...)."""
    h, w = img.shape[:2]
    x0 = max(0, int(rect[0] * w))
    y0 = max(0, int(rect[1] * h))
    x1 = min(w, int(rect[2] * w))
    y1 = min(h, int(rect[3] * h))
    if x1 <= x0 or y1 <= y0:
        return img
    return img[y0:y1, x0:x1]
