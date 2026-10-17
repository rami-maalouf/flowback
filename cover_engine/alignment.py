import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

import numpy as np

from .models import Candidate, Word


@dataclass(frozen=True)
class Match:
    candidate: Candidate
    pairs: list[tuple[int, int]]


@dataclass(frozen=True)
class Phrase:
    source_start: float
    source_end: float
    target_start: float
    target_end: float


def token(text: str) -> str:
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text).casefold())


@lru_cache(maxsize=16384)
def similarity(left: str, right: str) -> float:
    if left == right and left:
        return 1.0
    if min(len(left), len(right)) < 4:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    return ratio if ratio >= 0.8 else 0.0


def match_lyrics(reference: list[Word], performance: list[Word]) -> list[Match]:
    if len(performance) < 3 or len({token(w.text) for w in performance}) < 3:
        raise ValueError("Provide at least three distinct sung words, ideally a full line.")
    if not reference:
        raise ValueError("No confident lyrics match: the original vocals could not be transcribed.")
    query = [token(w.text) for w in performance]
    song = [token(w.text) for w in reference]
    rows, columns = len(query), len(song)
    scores = np.zeros((rows + 1, columns + 1), dtype=np.float32)
    directions = np.zeros((rows + 1, columns + 1), dtype=np.uint8)
    scores[:, 0] = -0.9 * np.arange(rows + 1)
    directions[1:, 0] = 1
    for i in range(1, rows + 1):
        for j in range(1, columns + 1):
            likeness = similarity(query[i - 1], song[j - 1])
            options = (
                scores[i - 1, j - 1] + (1.6 * likeness if likeness else -0.9),
                scores[i - 1, j] - 0.9,
                scores[i, j - 1] - 0.7,
            )
            direction = max(range(3), key=options.__getitem__)
            scores[i, j] = options[direction]
            directions[i, j] = direction

    matches = []
    for endpoint in sorted(range(1, columns + 1), key=lambda j: -scores[rows, j]):
        if scores[rows, endpoint] < rows * 0.45:
            break
        i, j = rows, endpoint
        pairs = []
        while i and j:
            direction = directions[i, j]
            if direction == 0:
                if similarity(query[i - 1], song[j - 1]):
                    pairs.append((i - 1, j - 1))
                i, j = i - 1, j - 1
            elif direction == 1:
                i -= 1
            else:
                j -= 1
        pairs.reverse()
        coverage = len(pairs) / rows
        if len(pairs) < 3 or coverage < 0.6:
            continue
        if len({query[i] for i, _ in pairs}) < 3:
            continue
        first_i, first_j = pairs[0]
        last_i, last_j = pairs[-1]
        source_span = performance[last_i].end - performance[first_i].start
        target_span = reference[last_j].end - reference[first_j].start
        if source_span <= 0 or not 0.5 <= target_span / source_span <= 2:
            continue
        scale = target_span / source_span
        start = max(
            0.0,
            reference[first_j].start - (performance[first_i].start - performance[0].start) * scale,
        )
        end = reference[last_j].end + (performance[-1].end - performance[last_i].end) * scale
        accuracy = sum(similarity(query[i], song[j]) for i, j in pairs) / len(pairs)
        skipped = max(0, last_j - first_j + 1 - rows) / rows
        score = max(0.0, 0.85 * coverage + 0.15 * accuracy - 0.15 * skipped)
        if score < 0.65:
            continue
        if any(
            min(end, m.candidate.end) - max(start, m.candidate.start)
            > 0.5 * min(end - start, m.candidate.end - m.candidate.start)
            for m in matches
        ):
            continue
        lyrics = " ".join(w.text.strip() for w in reference[first_j : last_j + 1])
        minutes, seconds = divmod(int(start), 60)
        candidate = Candidate(
            f"section-{first_j}-{last_j}",
            f"{minutes}:{seconds:02d}",
            start,
            end,
            coverage,
            score,
            lyrics,
        )
        matches.append(Match(candidate, pairs))
    if not matches:
        raise ValueError(
            "No confident lyrics match. Use a clearer vocal file with a full line "
            "of the original lyrics and a similar delivery speed."
        )
    return sorted(matches, key=lambda m: (-m.candidate.score, m.candidate.start))


def phrase_plan(
    reference: list[Word], performance: list[Word], pairs: list[tuple[int, int]]
) -> list[Phrase]:
    points = []
    for i, j in pairs:
        for source, target in (
            (performance[i].start, reference[j].start),
            (performance[i].end, reference[j].end),
        ):
            if not points or (source > points[-1][0] and target > points[-1][1]):
                points.append((source, target))
    if len(points) < 2:
        raise ValueError("Insufficient word timing to align this performance. Try a clearer file.")
    xs, ys = np.array(points).T
    scale = (ys[-1] - ys[0]) / (xs[-1] - xs[0])

    def mapped(value):
        if value < xs[0]:
            return float(ys[0] + (value - xs[0]) * scale)
        if value > xs[-1]:
            return float(ys[-1] + (value - xs[-1]) * scale)
        return float(np.interp(value, xs, ys))

    groups = []
    first = 0
    for i in range(1, len(performance)):
        gap = performance[i].start - performance[i - 1].end
        length = performance[i - 1].end - performance[first].start
        if (gap >= 0.3 and i - first >= 2) or (length >= 7 and i - first >= 4):
            groups.append((first, i - 1))
            first = i
    groups.append((first, len(performance) - 1))
    return [
        Phrase(
            performance[a].start,
            performance[b].end,
            max(0.0, mapped(performance[a].start)),
            mapped(performance[b].end),
        )
        for a, b in groups
    ]
