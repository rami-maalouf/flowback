# Vocal practice assessment: evidence and implementation

Research date: 17 October 2026.

## What already exists

There are usable pitch trackers, karaoke scoring systems, speech pronunciation services, and research systems that assess singing. The sources below do not establish a ready-made, validated score for improving articulation in arbitrary rap recordings. Flowback therefore implements separate, inspectable measurements rather than a single grade for vocal ability.

| System or research | What it provides | Fit for Flowback |
| --- | --- | --- |
| [Automatic Pronunciation Evaluation of Singing, Gupta et al. (2018)](https://www.isca-archive.org/interspeech_2018/gupta18_interspeech.html) | Singing-adapted recognition, lyric alignment, and phonetic posteriorgram comparison against human judgments. | Directly relevant research. Its small singing evaluation does not establish reliable pronunciation feedback for fast rap or arbitrary accents. The [associated dataset](https://github.com/chitralekha18/Dataset-for-pronunciation-evaluation-in-singing) is for research use. |
| [MusicJudge (2026 preprint)](https://arxiv.org/html/2606.26451v1) | A combined lyric, pitch, and rhythm framework; reports Spearman correlation 0.683 with expert judgments on its SwaraLyrics evaluation. | Promising direction, not a drop-in rap assessment model. Its singing genres and transcription errors matter; no deployable inference package was identified in the reviewed materials. Its weights for a combined score cannot be assumed valid for this app. |
| [PESnQ technical framework](https://www.cambridge.org/core/journals/apsipa-transactions-on-signal-and-information-processing/article/technical-framework-for-automatic-perceptual-evaluation-of-singing-quality/5F6AECB907FE842481D070850EDF1EFA) | Models perceptual singing dimensions and their relationship to human ratings. | Supports keeping different dimensions distinct and validating any later aggregate with listeners. |
| [UltraStar Deluxe](https://github.com/UltraStar-Deluxe/USDX) and its [song format](https://github.com/UltraStar-Deluxe/format/blob/main/The%20UltraStar%20File%20Format%20%28v2%29.md) | Mature karaoke gameplay using authored note and lyric timing. The format has rap notes for which pitch is ignored. | Useful UX precedent. Game points are not evidence of articulation improvement, and arbitrary audio still needs reliable reference annotation. No GPL implementation code is incorporated. |
| [Cantamus intonation assessment](https://github.com/helenacuesta/intonation-assessment) | Open-source score-based pitch comparison, selected passages, alignment offset, and note feedback. | Useful precedent for visual evidence and bounded excerpts. It does not solve rap articulation or automatic lyric assessment. |
| [Singing Ability Assessment research](https://pmc.ncbi.nlm.nih.gov/articles/PMC11289018/) and [musicassessr](https://github.com/sebsilas/musicassessr) | Structured singing tasks and assessment infrastructure. | Demonstrates the distinction between validated testing tasks and unrestricted commercial-song comparison. |
| [Automatic rhythm imitation assessment](https://github.com/barisbozkurt/auto-assess-rhythm-imitation) | Expert-annotated rhythm imitation and computational assessment. | Supports testing timing against known patterns. Grades learned for that task do not transfer automatically to expressive rap. |
| [MCFlow](https://github.com/Computational-Cognitive-Musicology-Lab/MCFlow) | Symbolic rap rhythm and rhyme annotations. | Relevant to future rhythm research, not a novice-skill scoring model. Matching every syllable to the nearest beat would wrongly penalize deliberate syncopation. |
| [Azure Pronunciation Assessment](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/speech-service/pronunciation-assessment/characteristics-and-limitations-pronunciation-assessment) | Speech pronunciation and fluency assessment under documented recording and language conditions. | Its guidance targets normal speech; rapid rap, sustained singing, and separated musical vocals fall outside those conditions. A numerical response would not establish validity here. |
| [GOPT](https://github.com/YuanGongND/gopt) and [SpeechOcean762](https://us.openslr.org/101/) | Learned pronunciation assessment for non-native spoken English. | The training/evaluation domain is read speech, not rap. No speech model is relabeled as a singing coach. |
| [SingMOS](https://github.com/South-Twilight/SingMOS) and [SingMOS-Pro](https://huggingface.co/datasets/TangRain/SingMOS-Pro) | Perceptual quality assessment of generated singing. | Naturalness of synthesis is a different target from a person's lyric delivery, cadence, or practice progress. |

## Which building blocks to use

[librosa pYIN](https://librosa.org/doc/0.11.0/generated/librosa.pyin.html) supplies fundamental-frequency estimates, voiced flags, and voicing probabilities. It was the first implementation because it is local, CPU-compatible, and already available in the audio environment. It passed controlled harmonic-audio tests but retained less than 1% of the separated reference's frames in the supplied rap excerpt under the chosen conservative gate. Longer windows did not resolve this in diagnostic crops. This is a result on that recording, not a general ranking of pitch trackers.

The release uses the full [torchcrepe](https://github.com/maxrmorrison/torchcrepe) model. Its documentation discusses silence gating and octave errors. Our synthetic controls and real-rap diagnostic crops supported investigating it: substantially more usable frames at the same numerical periodicity threshold, with silence and white noise still rejected. These confidence values are not calibrated across estimators, so equal thresholds do not establish equal accuracy. The implementation uses deterministic weighted framewise decoding, bounded CPU batches, a relative energy gate, and both raw and locally median-filtered periodicity. It avoids random pitch-bin dither. A broad-range Viterbi path failed on this reference, and restricting every song to a rap-specific pitch ceiling was rejected. Model-supported bounds and abstention remain explicit.

[RMVPE](https://github.com/Dream-High/RMVPE) is another candidate for difficult recordings; dependency cost, model provenance, and measured improvement should be evaluated before adoption. It does not remove the need for abstention and validation. [PESTO](https://github.com/SonyCSLParis/pesto) is another efficient pitch-estimation option, with a different software license.

Flowback reuses its existing word timestamps and preserves optional recognition probabilities for new transcripts. Adding [WhisperX](https://github.com/m-bain/whisperX) could improve timestamp alignment in supported languages, but forced alignment locates supplied words; it does not prove that they were pronounced correctly. The [Whisper model card](https://github.com/openai/whisper/blob/main/model-card.md) documents hallucinations and uneven performance across populations. Both the reference transcript and the user's transcript can be wrong.

The torchcrepe package supplies pretrained CREPE weights under its distribution terms; no singing-assessment training datasets are bundled or used to train a custom scorer. Dependencies retain their licenses. A later training effort needs a separate review of dataset and model-weight permissions; a permissive code license does not license every recording linked by its repository.

## What the current measurements mean

All measurements use the decoded original performance and separated reference vocal. The corrected, time-stretched listening mix is never a measurement input. Otherwise the app would score its own timing correction instead of the user's performance.

| Measurement | Definition | Interpretation and limits |
| --- | --- | --- |
| Transcript agreement | Exact ordered token matches divided by the longer transcript's token count within the matched excerpt. | A recognition agreement percentage, not a pronunciation or articulation score. Inspect both transcripts and listen before interpreting a mismatch. |
| Timing difference | Median absolute difference between matched raw word starts after subtracting the median constant recording-start offset. | Milliseconds of residual timing variation relative to this rendition. It preserves tempo drift. It does not measure absolute beat placement or label expressive differences as wrong. |
| Relative pace | Reference matched-anchor duration divided by performance matched-anchor duration, multiplied by 100. | 100% means the same overall pace; below 100% means slower. An overall ratio can hide local rushing and pauses, so inspect word timing too. |
| Pitch movement difference | Median absolute difference between paired word-median semitone pitches after removing a constant register offset. | Measures contour similarity while allowing a different comfortable register. Word medians are coarse and can miss within-word movement; intentional melodic variations remain differences. |
| Observed pitch span | 95th minus 5th percentile of usable pitch estimates, in semitones, shown for both excerpts. | The span heard in this excerpt, not physiological vocal range, voice type, or a target the user must force their voice to reach. |

Timing needs at least six reliable exact word anchors spanning two seconds. When ASR probabilities exist, low-probability anchors are excluded. Legacy transcripts without probabilities remain usable with an explicit evidence warning. Exact matching is separate from the fuzzy section locator, whose candidate score is never presented as vocal accuracy.

Pitch extraction uses energy and periodicity gates. It requires enough usable duration, coverage, and paired words before returning a comparison. Periodicity is not a probability that the estimated fundamental frequency is correct. Boundary crowding can make a measurement unavailable; octave-like discontinuities are reported rather than silently corrected. These are conservative engineering defaults, not thresholds calibrated against teachers or listeners. Voiceless consonants are normal, especially in rap; pitch unavailability is not poor performance.

The [mir_eval melody definitions](https://mir-eval.readthedocs.io/latest/api/melody.html) distinguish voicing, pitch, and chroma accuracy. They are useful when validating an estimator against annotated fundamental frequency. An estimator's pitch tolerance does not validate a human singing grade. Likewise, [work on perceptually meaningful pitch correction](https://arxiv.org/abs/2010.12196) cautions against treating extraction performance as equivalent to perceptual success.

## Listening and comparison design

The library groups covers by the exact imported original song. Each take keeps its recording, levels, export, selected section, and analysis. Reusing a source avoids repeating successful reference separation and transcription when the model configuration is unchanged.

The song screen groups a compact take list, the selected take's listening controls, and its measurements. A user can sort by one dimension, inspect the evidence, and compare to the original. Different sections are identified and excluded from the selected section's metric ranking; unavailable values appear last. Newer methods do not silently compete with older cached measurements.

Switching takes preserves position in original-song coordinates when the excerpts overlap. Independent raw vocal players expose what was actually measured, with the original reference labeled as separated audio. They pause the mixed playback and each other. This helps hear consonants and rhythm that accompaniment or timing correction could mask.

Analysis runs separately from rendering. A failed analysis does not make a saved cover unplayable. Changing live volume does not trigger another measurement or overwrite another take's settings. Results are cached by raw inputs, selected section, and method version. Partial or stale jobs cannot publish an assessment for a different selected section.

## Validation and honest next steps

Synthetic audio tests establish specific computational properties: identical input matches; gain changes and leading silence do not worsen comparison; constant transposition preserves pitch shape; tempo drift changes timing and pace; restricted pitch movement changes the observed span; silence and noise do not earn invented pitch scores. Service tests cover persistence, failures, section changes, sorting, and bounded raw-audio playback. These tests establish implementation behavior, not improvement in a person's speech or musical ability.

The supplied real recording is a practical execution check. Inspecting its pitch coverage and recognition evidence can reveal failure modes; a single recording cannot establish general accuracy. Reference separation artifacts, ad-libs, backing leakage, room noise, accents, rapid delivery, and repeated lyrics all remain important sources of error.

Before introducing an articulation grade or an overall score:

1. Collect consented, legally usable recordings across rap speeds, accents, languages, microphone conditions, and speaking/singing styles. Hold out both performers and songs during evaluation.
2. Obtain blinded, repeated ratings from multiple listeners or coaches for clearly defined dimensions. Measure agreement between humans before judging a model against them.
3. Independently annotate a subset of word boundaries, phonetic errors, and fundamental-frequency tracks. Measure timing error, pitch octave errors, missing-data rates, and variation across groups.
4. Validate confidence and abstention rules, not just average correlation. Test whether an apparent improvement exceeds normal repeat-recording and estimator variation.
5. Run longitudinal practice studies before claiming transfer to everyday articulation. Keep user listening and self-assessment available even if a future learned score is added.

Current release claim: a saved, reproducible comparison of a vocal take with a selected reference excerpt, with explicit evidence and limitations. It does not diagnose a speech issue, judge rhyme quality, establish the user's full vocal range, or promise that matching the original singer improves articulation.
