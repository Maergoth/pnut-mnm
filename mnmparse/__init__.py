"""mnmparse: a passive, out-of-process combat-log reader for Monsters & Memories.

The game has no combat log file, so this package reads the in-game "Combat"
chat window off the screen (Windows Graphics Capture or a desktop region grab),
OCRs it, de-duplicates the scrolling text and writes an EverQuest-style log.
Nothing in this package touches the game process: no memory reads, nothing
loaded into it, no input or window messages sent to it.
"""

__version__ = "0.1.6"
