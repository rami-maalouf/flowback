"""shared data at the audio engine boundary."""

from dataclasses import dataclass
from typing import Callable

Progress = Callable[[str, str], None]


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str
    start: float
    end: float
    coverage: float
    score: float
    lyrics: str


@dataclass(frozen=True)
class RenderResult:
    start: float
    end: float
    duration: float
    warning: str
