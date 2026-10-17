import pytest

from cover_engine.alignment import match_lyrics, phrase_plan
from cover_engine.models import Word


def words(text, offset=0.0, step=0.5):
    return [
        Word(t, offset + i * step, offset + i * step + step * 0.75)
        for i, t in enumerate(text.split())
    ]


def test_partial_performance_finds_song_section():
    song = words("a quiet introduction before we chase the sunlight through the city tonight")
    vocal = words("we chase the sunlight through the city", offset=1, step=0.6)
    matches = match_lyrics(song, vocal)
    assert matches[0].candidate.start == pytest.approx(2)
    assert matches[0].candidate.end == pytest.approx(5.375)
    assert matches[0].candidate.coverage == 1
    assert len(matches) == 1


def test_repeated_chorus_keeps_separate_candidates():
    chorus = "we chase the sunlight through the city tonight"
    song = words(chorus, offset=10) + words("another verse entirely", offset=20)
    song += words(chorus, offset=30)
    matches = match_lyrics(song, words(chorus))
    assert len(matches) == 2
    assert [m.candidate.start for m in matches] == [10, 30]
    assert matches[0].candidate.score == matches[1].candidate.score


def test_one_misheard_word_does_not_discard_the_section():
    song = words("we chase the sunlight through the city tonight", offset=12)
    vocal = words("we chase the sunrise through the city tonight")
    match = match_lyrics(song, vocal)[0]
    assert match.candidate.start == 12
    assert match.candidate.coverage >= 0.8


def test_unrelated_words_are_refused():
    with pytest.raises(ValueError, match="confident"):
        match_lyrics(
            words("we chase the sunlight through the city tonight"),
            words("the recipes require flour salt and baking powder"),
        )


def test_too_few_words_are_refused():
    with pytest.raises(ValueError, match="three"):
        match_lyrics(words("we chase the sunlight"), words("we chase"))


def test_phrase_plan_preserves_groups_of_words():
    vocal = words("we chase the sunlight", step=0.35)
    vocal += words("through the city tonight", offset=2.5, step=0.35)
    song = words("we chase the sunlight", offset=10, step=0.4)
    song += words("through the city tonight", offset=13, step=0.4)
    match = match_lyrics(song, vocal)[0]
    phrases = phrase_plan(song, vocal, match.pairs)
    assert len(phrases) == 2
    assert phrases[0].source_start == 0
    assert phrases[0].target_start == 10
    assert phrases[-1].target_end == pytest.approx(song[-1].end)
