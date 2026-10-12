"""chatscrub: anonymize chat exports into privacy-safe corpora."""

from .scrub import ScrubConfig, CompiledScrub, clean_text, real_text_len
from .pipeline import PipelineConfig, run, room_label

__version__ = "0.1.0"
__all__ = [
    "ScrubConfig",
    "CompiledScrub",
    "PipelineConfig",
    "run",
    "room_label",
    "clean_text",
    "real_text_len",
]
