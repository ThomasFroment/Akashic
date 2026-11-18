# Speech and speakers

## Quality decisions

Transcription quality takes priority over speed. Deferred processing is acceptable; choosing a
smaller, faster or quantized model needs evidence that French, English and mixed speech retain
meaning. A selectable model and a successful generated-speech smoke test are not quality proof.

Vocabulary hints can help recurring names and game terms but can also produce prompt echoes.
Do not silently rewrite transcripts with an LLM or fuzzy name replacement. Retain the source
recording, distinguish uncertain recognition and measure hallucinations as well as omissions.

A long utterance is cut and continued. The continuation keeps any fresh speech, however short,
and still goes through the quiet check, which judges the whole voiced level, the 2 s overlap
included ([continuation on read speech](baselines/2026-11-17-continuation-speech.md)).

The microphone represents the user. System audio may mix friends, videos, music and game sound;
process participation does not identify the author of every word.

## Identity and attribution

Finding a person's participation in a recording does not establish which sentence they said.
Keep mixed or uncertain authorship explicit, and do not present a whole mixed transcript as a
quotation from one participant. Speaker corrections and merges must update derived associations
without silently changing unrelated identities.

A system segment names only the processes audible where it was recorded, so voice chat routed
elsewhere is missing from it ([CAPTURE.md](CAPTURE.md#audio)). The
microphone's speaker is the user without any voice model. System segments wait for the voice
models, and after they are downloaded every transcribed system segment that still has its audio
gets one speaker job.

Every segment shown to the user carries a speech tag, derived at read time and never stored. The
first rule that fits wins: the microphone stream is `you`; a source that is a voice app
(`VOICE_APPS`) is `voice chat`; a source process with an overlapping game session is `game`; any
other source is `media`; no source gives no tag. It is shown in Search > Speech, the Memory lines
and Ask ("Minlo said (voice chat)"); mic lines get no suffix. The tag says what was audible, not
who spoke. Conversation building applies the same rule to the sources alone, whatever the stream,
so mic speech with a voice app audible still joins a call.

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
