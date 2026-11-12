# Speech and speakers

## Quality decisions

Transcription quality takes priority over speed. Deferred processing is acceptable; choosing a
smaller, faster or quantized model needs evidence that French, English and mixed speech retain
meaning. A selectable model and a successful generated-speech smoke test are not quality proof.

Vocabulary hints can help recurring names and game terms but can also produce prompt echoes.
Do not silently rewrite transcripts with an LLM or fuzzy name replacement. Retain the source
recording, distinguish uncertain recognition and measure hallucinations as well as omissions.

The microphone represents the user. System audio may mix friends, videos, music and game sound;
process participation does not identify the author of every word.

## Identity and attribution

Finding a person's participation in a recording does not establish which sentence they said.
Keep mixed or uncertain authorship explicit, and do not present a whole mixed transcript as a
quotation from one participant. Speaker corrections and merges must update derived associations
without silently changing unrelated identities.

Word alignment and diarization are separate sources of uncertainty, especially on short replies,
continuations and overlapping voices. Public read speech or synthetic voices cannot calibrate
real Discord/headset attribution by themselves.

## Comparisons

Compare candidates on the same independently corrected recordings and processing conditions.
Include French, English, code-switching, names, game terms, negations, quiet/short speech and
overlap. Inspect meaning-changing errors separately from aggregate word error rates; failures
and non-speech clips must not disappear from the comparison.

Keep references, predictions and identifying voice features local and encrypted. Comparisons
must not replace ordinary memories. Transcript deletion must propagate to these copies too
([STORAGE.md](STORAGE.md)). Read the [quality audit](audits/2026-11-10-2.md) and matching
[method reports](solutions/) before changing recognition or attribution; their evaluation gaps
and candidate results remain relevant.

A comparison reference must be corrected independently of the draft it started from, and its
language and recording condition must be labelled. Comparisons run through the same processing
boundary as production. Panel and scoring mechanics live in `pipeline/stt_compare.py`,
`api/stt.py` and their tests.
