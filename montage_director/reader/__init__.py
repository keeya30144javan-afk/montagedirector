"""The Free Director's prompt reader.

Reads a plain-English idea the way an editor would, in four steps (one file each):

1. normalise.py   - tidy it up ("1 minute 30" -> 1:30, "0:30-0:45" -> one range, "last 20 seconds", typos)
2. splitting.py   - split it into instructions (sentences, commas, "then", "and" before a new part of the song)
3. instruction.py - for each instruction find WHERE it applies (whole edit, a part of the song like "the
                    chorus", a moment the app hears like "when the solo starts", "the second drop", a time
                    or range) and WHAT it asks for (pace, effects with amounts, negation, the kind of shots,
                    cutting on the kick or snare, effects on drum hits, holds, speed ramps, "freeze at 1:05")
4. apply.py       - turn them into a Direction: whole edit first, then parts of the song, then timed
                    sections, so the more specific instruction always wins

vocabulary.py holds every word list (start there to teach it a new phrase), spelling.py the
"did you mean…?" suggestions, and typing_help.py the speech-bubble tips shown while typing. Anything it couldn't read is reported back so the
user knows to rephrase it.
"""

from .apply import extract_texts, read_prompt, read_timeline, region_direction
from .instruction import Instruction, read_instruction
from .normalise import TIME_RE, normalise
from .spelling import did_you_mean
from .splitting import _split, _split_and
from .typing_help import typing_help

__all__ = ["Instruction", "TIME_RE", "_split", "_split_and", "did_you_mean", "extract_texts", "normalise", "read_instruction",
           "read_prompt", "read_timeline", "region_direction", "typing_help"]
