from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MPL_CACHE_DIR = PROJECT_DIR / ".cache" / "matplotlib"
PREFERRED_KOREAN_FONTS = [
    "AppleGothic",
    "Pretendard Variable",
    "Pretendard",
    "NanumGothic",
    "Noto Sans CJK KR",
    "Noto Sans KR",
    "Malgun Gothic",
]


def ensure_matplotlib_env(cache_dir: Path | None = None) -> Path:
    resolved = Path(cache_dir) if cache_dir is not None else DEFAULT_MPL_CACHE_DIR
    resolved.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(resolved)
    return resolved


def configure_matplotlib(matplotlib_module: object) -> str:
    font_name = "DejaVu Sans"
    try:
        from matplotlib import font_manager

        installed_fonts = {font.name for font in font_manager.fontManager.ttflist}
        for candidate in PREFERRED_KOREAN_FONTS:
            if candidate in installed_fonts:
                font_name = candidate
                break
    except Exception:
        pass

    matplotlib_module.rcParams["font.family"] = [font_name]
    matplotlib_module.rcParams["axes.unicode_minus"] = False
    return font_name


configure_korean_matplotlib_font = configure_matplotlib
