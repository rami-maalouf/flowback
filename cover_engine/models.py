from collections.abc import Callable
from dataclasses import dataclass

Progress = Callable[[str, str], None]


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    probability: float | None = None


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
