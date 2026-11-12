"""Score a hallucination-rejection candidate on the cached production fixture.

Run:  .venv\\Scripts\\python.exe -I harness.py <candidate.py> [--name NAME] [--split test]

A candidate module exposes
    judge(clip_cache: dict, helpers: Helpers) -> {"status": "keep"|"unclear"|"reject",
        "text": stored_text_after_stripping, "extra_decodes": int, "extra_ms": float}
and optionally ``fit(mining: list[tuple[dict, dict]], helpers)``, called once with the mining
split (clip_cache, label) pairs before judging. ``clip_cache`` holds no labels.

Writes <HERE>/<name>/metrics.txt (one line), details.json and side.png.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

import prod  # noqa: E402

CACHE = HERE / "data" / "cache"
WINDOW_MS = 32
LABEL_KEYS = ("role", "speech_coverage", "kind", "subset", "lang_true", "opus", "orig_id", "split",
              "group", "ref_text", "names", "names_in_vocab", "seg_id", "clip_id")
TAIL_SLACK_S = 0.2


# ---------------------------------------------------------------- data
def load_cache() -> list[tuple[dict, dict]]:
    out = []
    vad_cache: dict[str, np.ndarray] = {}
    for line in (CACHE / "segments.jsonl").open(encoding="utf-8"):
        rec = json.loads(line)
        label = {k: rec[k] for k in LABEL_KEYS}
        cc = {k: v for k, v in rec.items() if k not in LABEL_KEYS}
        cc["id"] = hashlib.sha1(rec["seg_id"].encode()).hexdigest()[:12]
        if rec["clip_id"] not in vad_cache:
            vad_cache = {rec["clip_id"]: np.load(CACHE / "vad" / f"{rec['clip_id']}.npy")}
        probs = vad_cache[rec["clip_id"]]
        cc["vad_probs"] = probs[rec["start_ms"] // WINDOW_MS : rec["end_ms"] // WINDOW_MS].tolist()
        cc["duration_s"] = (rec["end_ms"] - rec["start_ms"]) / 1000
        out.append((cc, label))
    return out


class Helpers:
    """Everything a candidate may use. Model work through these methods is counted."""

    def __init__(self, ids: dict[str, str]) -> None:
        self._ids = ids  # anonymised id -> seg_id (for the audio file only)
        self._whisper: prod.Whisper | None = None
        self.decodes = 0
        self.encodes = 0
        self.repo = SimpleNamespace(
            normalize=prod.normalize, is_hallucination=prod.is_hallucination,
            HALLUCINATIONS=prod.repo_transcribe.HALLUCINATIONS, build_prompt=prod.build_prompt,
            confidence=prod.confidence, is_unclear=prod.is_unclear,
            UNCLEAR_BELOW=prod.repo_transcribe.UNCLEAR_BELOW,
            PROMPT_MAX_CHARS=prod.repo_transcribe.PROMPT_MAX_CHARS,
            PREVIOUS_MAX_CHARS=prod.repo_transcribe.PREVIOUS_MAX_CHARS,
            NO_SPEECH_THRESHOLD=prod.repo_whisper.NO_SPEECH_THRESHOLD,
            LOG_PROB_THRESHOLD=prod.repo_whisper.LOG_PROB_THRESHOLD,
            COMPRESSION_RATIO_THRESHOLD=prod.repo_whisper.COMPRESSION_RATIO_THRESHOLD,
            normalize_loudness=prod.normalize_loudness,
        )
        self.mining: list[tuple[dict, dict]] = []

    # audio
    def samples(self, cc: dict) -> np.ndarray:
        """The segment as stored (Ogg/Opus 24 kbps round trip), 16 kHz float32."""
        return np.load(CACHE / "audio" / f"{self._ids[cc['id']]}.npy")

    def whisper_input(self, cc: dict) -> np.ndarray:
        """Exactly what Whisper received: normalize_loudness(stored samples)."""
        return prod.normalize_loudness(self.samples(cc))

    def silero(self, samples: np.ndarray) -> np.ndarray:
        """Fresh-state Silero v6 probability per 32 ms window (CPU, cheap; not counted)."""
        return prod.silero_probs(samples)

    # model
    @property
    def whisper(self) -> prod.Whisper:
        if self._whisper is None:
            self._whisper = prod.Whisper()
        return self._whisper

    @property
    def model(self):  # noqa: ANN201 - faster_whisper.WhisperModel (loaded on first use)
        return self.whisper.model

    @property
    def ct2(self):  # noqa: ANN201 - ctranslate2.models.Whisper
        return self.whisper.model.model

    def decode(self, samples: np.ndarray, prompt: str | None, language: str | None = None, **overrides) -> list:
        """faster-whisper transcribe with the production options (overridable). Counted."""
        self.decodes += 1
        return self.whisper.decode(samples, prompt, language, **overrides)

    def production(self, samples: np.ndarray, prompt: str | None) -> dict:
        """Language detection + production decode, every field kept. Counted as one decode."""
        self.decodes += 1
        out = self.whisper.production(samples, prompt)
        out.pop("_raw", None)
        return out

    def detect_language(self, samples: np.ndarray) -> tuple[str, dict]:
        self.encodes += 1
        return self.whisper.detect(samples)

    def align_words(self, samples: np.ndarray, parts: list[dict], language: str) -> list[dict]:
        self.encodes += 1
        return self.whisper.align_words(samples, parts, language)

    def encode(self, samples: np.ndarray):  # noqa: ANN201 - ctranslate2.StorageView
        """Encoder output of the first 30 s window. Counted as an encode."""
        from faster_whisper.audio import pad_or_trim
        self.encodes += 1
        feats = self.model.feature_extractor(samples)
        return self.model.encode(pad_or_trim(feats[:, : self.model.feature_extractor.nb_max_frames]))


# ---------------------------------------------------------------- scoring
def words(text: str) -> list[str]:
    return prod.normalize(text or "").split()


def matched(ref: list[str], hyp: list[str]) -> int:
    return sum(b.size for b in SequenceMatcher(None, ref, hyp, autojunk=False).get_matching_blocks())


def trailing_extra(ref: list[str], hyp: list[str]) -> int | None:
    """Words after the end of the reference; None when the end of the reference is not found."""
    if not hyp:
        return 0
    blocks = [b for b in SequenceMatcher(None, ref, hyp, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return None
    last = blocks[-1]
    ref_end = last.a + last.size
    if ref_end < len(ref) - 2:
        return None
    return max(0, len(hyp) - (last.b + last.size) - (len(ref) - ref_end))


def wilson_upper(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return float("nan")
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / (1 + z * z / n))


def rate(rows: list[dict], key: str) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r[key])
    return {"k": k, "n": n, "rate": k / n if n else float("nan"), "ub95": wilson_upper(k, n)}


def evaluate(units: list[tuple[dict, dict]], verdicts: list[dict]) -> tuple[dict, list[dict]]:
    rows = []
    for (cc, lab), v in zip(units, verdicts, strict=True):
        raw_w, kept_w = words(cc["text"]), words(v["text"])
        status = v["status"] if kept_w else "reject"
        row = {"seg_id": lab["seg_id"], "role": lab["role"], "subset": lab["subset"],
               "lang": lab["lang_true"], "opus": lab["opus"], "today": cc["today"], "status": status,
               "raw_text": cc["text"], "stored_text": v["text"] if status != "reject" else "",
               "extra_decodes": v["extra_decodes"], "judge_ms": v["judge_ms"],
               "decode_ms": cc["decode_ms"]}
        if lab["role"] == "nonspeech":
            row["halluc"] = bool(raw_w)
            row["caught"] = row["halluc"] and status in ("reject", "unclear")
            row["removed"] = row["halluc"] and status == "reject"
        else:
            ref = words(lab["ref_text"])
            m_raw = matched(ref, raw_w)
            m_kept = matched(ref, kept_w) if status != "reject" else 0
            row.update(ref_text=lab["ref_text"], matched_raw=m_raw, matched_kept=m_kept)
            row["false_reject"] = status == "reject" or m_kept < m_raw
            row["rejected"] = status == "reject"
            row["stripped"] = status != "reject" and m_kept < m_raw
            row["unclear"] = status == "unclear"
            row["raw_ok"] = cc["today"] != "reject"
            extra_raw = trailing_extra(ref, raw_w)
            row["trailing_extra_raw"] = extra_raw
            if lab["role"] == "speech_tail":
                speech_end_s = None
                # timestamp evidence: a raw word starting inside the non-speech tail
                tail_words = []
                if cc.get("words"):
                    # the speech region ends where the clip's speech ends; the segment starts at start_ms
                    speech_end_s = lab["speech_end_rel_s"]
                    tail_words = [w for w in cc["words"] if w["start"] >= speech_end_s + TAIL_SLACK_S]
                row["tail_words_raw"] = len(tail_words)
                row["halluc"] = bool(extra_raw) and bool(tail_words)
                extra_kept = trailing_extra(ref, kept_w) if status != "reject" else 0
                row["trailing_extra_kept"] = extra_kept
                gone = status == "reject" or (extra_kept is not None and extra_kept == 0)
                row["caught"] = row["halluc"] and (gone or status == "unclear")
                row["removed"] = row["halluc"] and gone
        rows.append(row)

    speech = [r for r in rows if r["role"] != "nonspeech"]
    halluc_ns = [r for r in rows if r["role"] == "nonspeech" and r["halluc"]]
    halluc_tail = [r for r in rows if r["role"] == "speech_tail" and r["halluc"]]
    halluc = halluc_ns + halluc_tail

    def sub(pred) -> list[dict]:
        return [r for r in speech if pred(r)]

    subsets = {
        "all": speech,
        "short": sub(lambda r: r["subset"] == "short"),
        "names": sub(lambda r: r["subset"] == "names"),
        "normal": sub(lambda r: r["subset"] == "normal"),
        "concat": sub(lambda r: r["subset"].startswith("concat")),
        "opus": sub(lambda r: r["opus"]),
        "fr": sub(lambda r: r["lang"] == "fr"),
        "en": sub(lambda r: r["lang"] == "en"),
    }
    by_cat: dict[str, list[dict]] = {}
    for r in halluc_ns:
        by_cat.setdefault(r["subset"].split("/")[0], []).append(r)
    suspects = [r for r in rows if r["extra_decodes"] > 0]
    primary_ms = sum(r["decode_ms"] for r in rows)
    metrics = {
        "segments": len(rows),
        "nonspeech_segments": sum(r["role"] == "nonspeech" for r in rows),
        "hallucination_recall": rate(halluc, "caught"),
        "hallucination_reject_recall": rate(halluc, "removed"),
        "nonspeech_recall": rate(halluc_ns, "caught"),
        "nonspeech_reject_recall": rate(halluc_ns, "removed"),
        "tail_recall": rate(halluc_tail, "caught"),
        "tail_reject_recall": rate(halluc_tail, "removed"),
        "nonspeech_recall_by_source": {k: rate(v, "caught") for k, v in sorted(by_cat.items())},
        "frr": {k: rate(v, "false_reject") for k, v in subsets.items()},
        "frr_rejected_only": rate(speech, "rejected"),
        "frr_stripped_only": rate(speech, "stripped"),
        "frr_among_today_nonempty": rate([r for r in speech if r["raw_ok"]], "false_reject"),
        "unclear": {k: rate(v, "unclear") for k, v in subsets.items()},
        "speech_with_trailing_extra_raw": sum(1 for r in speech if r["role"] == "speech" and r["trailing_extra_raw"]),
        "extra_decodes_total": sum(r["extra_decodes"] for r in rows),
        "extra_decodes_max": max((r["extra_decodes"] for r in rows), default=0),
        "suspects": len(suspects),
        "extra_decodes_per_suspect": (sum(r["extra_decodes"] for r in suspects) / len(suspects)) if suspects else 0.0,
        "judge_ms_total": sum(r["judge_ms"] for r in rows),
        "primary_decode_ms_total": primary_ms,
        "extra_time_ratio": sum(r["judge_ms"] for r in rows) / primary_ms if primary_ms else 0.0,
    }
    return metrics, rows


def one_line(name: str, m: dict) -> str:
    f = m["frr"]
    return (
        f"{name} recall={m['hallucination_recall']['rate']:.3f} (n={m['hallucination_recall']['n']}) "
        f"reject_recall={m['hallucination_reject_recall']['rate']:.3f} "
        f"ns_recall={m['nonspeech_recall']['rate']:.3f} tail_recall={m['tail_recall']['rate']:.3f} "
        f"(n={m['tail_recall']['n']}) "
        f"FRR={f['all']['rate']:.4f} (ub95={f['all']['ub95']:.4f}, n={f['all']['n']}) "
        f"short={f['short']['rate']:.4f}/{f['short']['ub95']:.4f} names={f['names']['rate']:.4f}/{f['names']['ub95']:.4f} "
        f"opus={f['opus']['rate']:.4f}/{f['opus']['ub95']:.4f} fr={f['fr']['rate']:.4f}/{f['fr']['ub95']:.4f} "
        f"en={f['en']['rate']:.4f}/{f['en']['ub95']:.4f} "
        f"unclear={m['unclear']['all']['rate']:.3f} "
        f"extra_dec/suspect={m['extra_decodes_per_suspect']:.2f} max={m['extra_decodes_max']} "
        f"extra_time={m['extra_time_ratio']:.3f}x"
    )


# ---------------------------------------------------------------- figure
SURFACE, INK, INK2, GRID = "#1a1a19", "#f0efec", "#c3c2b7", "#3a3a37"
BLUE, ORANGE = "#3987e5", "#d95926"
FRR_TOP, UNCLEAR_TOP = 0.08, 0.6


def side_png(name: str, m: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), gridspec_kw={"width_ratios": [1.1, 1.6, 1.3]})
    fig.patch.set_facecolor(SURFACE)
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.tick_params(colors=INK2, labelsize=9)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)

    # 1: recall
    ax = axes[0]
    groups = [("all", "hallucination"), ("nonspeech", "nonspeech"), ("tail", "tail")]
    x = np.arange(len(groups))
    caught = [m[f"{g}_recall" if g != "all" else "hallucination_recall"]["rate"] for g, _ in groups]
    removed = [m[f"{g}_reject_recall" if g != "all" else "hallucination_reject_recall"]["rate"] for g, _ in groups]
    ns = [m[f"{g}_recall" if g != "all" else "hallucination_recall"]["n"] for g, _ in groups]
    b1 = ax.bar(x - 0.19, np.nan_to_num(caught), 0.36, color=BLUE, label="not clear (reject or unclear)")
    b2 = ax.bar(x + 0.19, np.nan_to_num(removed), 0.36, color=ORANGE, label="removed (reject / stripped)")
    for bars in (b1, b2):
        for b in bars:
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.015, f"{b.get_height():.2f}",
                    ha="center", color=INK, fontsize=8)
    ax.axhline(0.9, color=INK2, linestyle="--", linewidth=1)
    ax.text(len(groups) - 0.5, 0.905, "90% target", color=INK2, fontsize=8, ha="right", va="bottom")
    ax.set_xticks(x, [f"{lbl}\nn={n}" for (_, lbl), n in zip(groups, ns, strict=True)], color=INK2)
    ax.set_ylim(0, 1.08)
    ax.set_title("Hallucination recall", color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, labelcolor=INK2, fontsize=8, loc="upper center",
              bbox_to_anchor=(0.5, -0.16), ncol=1)

    # 2: FRR per subset
    ax = axes[1]
    keys = ["all", "short", "names", "normal", "concat", "opus", "fr", "en"]
    x = np.arange(len(keys))
    vals = [m["frr"][k]["rate"] for k in keys]
    ubs = [m["frr"][k]["ub95"] for k in keys]
    bars = ax.bar(x, np.nan_to_num(vals), 0.6, color=BLUE, label="false rejection")
    ax.errorbar(x, np.nan_to_num(vals), yerr=[np.zeros(len(keys)), np.nan_to_num(np.array(ubs) - np.array(vals))],
                fmt="none", ecolor=INK2, capsize=4, linewidth=1, label="Wilson 95% upper")
    top = FRR_TOP  # fixed scale so every candidate's figure compares at a glance
    for b, v, ub in zip(bars, vals, ubs, strict=True):
        y = min(np.nan_to_num(v), top * 0.9)
        ax.text(b.get_x() + b.get_width() / 2, y + top * 0.01, f"{v * 100:.1f}%\n(ub {ub * 100:.1f}%)",
                ha="center", va="bottom", color=INK, fontsize=7)
    ax.axhline(0.01, color=ORANGE, linestyle="--", linewidth=1)
    ax.text(-0.45, 0.0105, "1% limit", color=ORANGE, fontsize=8, ha="left", va="bottom")
    ax.set_xticks(x, [f"{k}\nn={m['frr'][k]['n']}" for k in keys], color=INK2)
    ax.set_ylim(0, top)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=1))
    ax.set_title("Real-speech false rejection (rejected or reference word stripped)", color=INK,
                 fontsize=11, loc="left")
    ax.legend(frameon=False, labelcolor=INK2, fontsize=8, loc="upper right")

    # 3: unclear rate
    ax = axes[2]
    vals = [m["unclear"][k]["rate"] for k in keys]
    bars = ax.bar(x, np.nan_to_num(vals), 0.6, color=BLUE)
    for b, v in zip(bars, vals, strict=True):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.01, f"{v * 100:.0f}%",
                ha="center", va="bottom", color=INK, fontsize=8)
    ax.set_xticks(x, keys, color=INK2)
    ax.set_ylim(0, UNCLEAR_TOP)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.set_title("Real speech flagged unclear", color=INK, fontsize=11, loc="left")

    fig.suptitle(f"{name}   extra decodes/suspect {m['extra_decodes_per_suspect']:.2f}   "
                 f"extra time {m['extra_time_ratio']:.2f}x primary decode", color=INK, fontsize=12,
                 x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=110, facecolor=SURFACE)
    plt.close(fig)


# ---------------------------------------------------------------- main
def load_candidate(path: Path):  # noqa: ANN201
    spec = importlib.util.spec_from_file_location(f"candidate_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path)
    ap.add_argument("--name", default=None)
    ap.add_argument("--split", default="test", choices=("test", "mining", "all"))
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    name = args.name or args.candidate.stem
    out = args.out or HERE / name
    out.mkdir(parents=True, exist_ok=True)

    data = load_cache()
    clips = {json.loads(l)["clip_id"]: None for l in (CACHE / "clips.jsonl").open(encoding="utf-8")}
    fixture = {json.loads(l)["id"]: json.loads(l) for l in (HERE / "data" / "fixture" / "clips.jsonl").open(encoding="utf-8")}
    for cc, lab in data:
        clip = fixture[lab["clip_id"]]
        region = clip["speech_ms"]
        lab["speech_end_rel_s"] = ((region[1] - cc["start_ms"]) / 1000) if region else None
    assert all(lab["clip_id"] in clips for _, lab in data)
    ids = {cc["id"]: lab["seg_id"] for cc, lab in data}
    helpers = Helpers(ids)
    helpers.mining = [(cc, lab) for cc, lab in data if lab["split"] == "mining"]
    candidate = load_candidate(args.candidate)
    if hasattr(candidate, "fit"):
        candidate.fit(helpers.mining, helpers)
    units = [(cc, lab) for cc, lab in data if args.split == "all" or lab["split"] == args.split]
    verdicts = []
    for cc, _lab in units:
        before = helpers.decodes
        t0 = time.perf_counter()
        v = candidate.judge(dict(cc), helpers)
        ms = (time.perf_counter() - t0) * 1000
        counted = helpers.decodes - before
        verdicts.append({"status": v["status"], "text": v.get("text", ""),
                         "extra_decodes": max(int(v.get("extra_decodes", 0)), counted),
                         "reported_extra_ms": float(v.get("extra_ms", 0.0)), "judge_ms": ms})
    metrics, rows = evaluate(units, verdicts)
    metrics["name"] = name
    metrics["split"] = args.split
    line = one_line(name, metrics)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    (out / "details.json").write_text(json.dumps({"metrics": metrics, "rows": rows}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    side_png(name, metrics, out / "side.png")
    print(line)


if __name__ == "__main__":
    main()
