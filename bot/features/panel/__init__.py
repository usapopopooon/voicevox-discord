"""control-panel feature の公開 API。"""

from .models import PanelSnapshot
from .presentation import build_panel_embed

__all__ = [
    "PanelSnapshot",
    "build_panel_embed",
]
