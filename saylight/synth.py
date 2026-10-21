"""Saylight 1.0's synthesis module, kept so existing imports keep working.

The code now lives elsewhere: SynthWorker is the online voice (voices.edge.EdgeBackend), the temporary
playback folder is audio_cache.AudioCache, and voices.router.SynthRouter puts every voice behind this same
SynthWorker interface.
"""

from .audio_cache import DIR_PREFIX, OLD_DIR_PREFIXES, STALE_DIR_AGE_S, purge_orphans, purge_stale_dirs
from .voices.base import Word
from .voices.edge import (CONNECT_TIMEOUT_S, IDLE_TIMEOUT_S, RECEIVE_TIMEOUT_S, IncompleteAudio, classify_error,
                          rate_string)
from .voices.edge import EdgeBackend as SynthWorker

__all__ = ["CONNECT_TIMEOUT_S", "DIR_PREFIX", "IDLE_TIMEOUT_S", "OLD_DIR_PREFIXES", "RECEIVE_TIMEOUT_S",
           "STALE_DIR_AGE_S", "IncompleteAudio", "SynthWorker", "Word", "classify_error", "purge_orphans",
           "purge_stale_dirs", "rate_string"]
