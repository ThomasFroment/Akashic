"""The quality harness (``eval/quality``): profile, scenario, coverage, store, scorer, compare.

Nothing here touches the desktop: no window, no audio, no launched app. ``eval/quality`` is not a
package; it is put on ``sys.path`` and imported by name, so its modules are typed as ``Any``.
"""

from __future__ import annotations

import copy
import getpass
import importlib
import io
import itertools
import json
import re
import statistics
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from PIL import Image

from akashic.storage.crypto import unwrap_data_key

QUALITY = Path(__file__).resolve().parents[1] / "eval" / "quality"
if str(QUALITY) not in sys.path:
    sys.path.append(str(QUALITY))
sc: Any = importlib.import_module("scenario")
scoring: Any = importlib.import_module("score")
tagmod: Any = importlib.import_module("tags")
assets: Any = importlib.import_module("assets")
runmod: Any = importlib.import_module("run")
common: Any = importlib.import_module("common")

MEASURED = "measured 2026-11-18 (1 h)"
HOUR = 3_600_000


def load(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def profile() -> dict[str, Any]:
    return load(QUALITY / "profile.toml")


@pytest.fixture(scope="module")
def coverage() -> dict[str, Any]:
    return load(QUALITY / "coverage.toml")


_cache: dict[tuple[str, int], dict[str, Any]] = {}


def scenario(length: str, seed: int = 5) -> dict[str, Any]:
    if (length, seed) not in _cache:
        _cache[length, seed] = json.loads(sc.generate(length, seed))
    return _cache[length, seed]


def focus_steps(scn: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    spans = [(b["t0_ms"], b["t1_ms"]) for b in scn["blocks"] if b["kind"] == kind]
    return [
        s
        for s in scn["steps"]
        if s["kind"] == "focus" and any(a <= s["t0_ms"] < b for a, b in spans)
    ]


# --- profile.toml --------------------------------------------------------------------------


def test_every_profile_leaf_has_value_and_source(profile: dict[str, Any]) -> None:
    found = dict(sc.leaves(profile))
    assert found
    for path, leaf in found.items():
        assert "value" in leaf, path
        assert leaf["source"] == "assumed" or leaf["source"] == MEASURED, path


def test_only_gaming_block_values_are_measured(profile: dict[str, Any]) -> None:
    for path, leaf in sc.leaves(profile):
        if leaf["source"] == MEASURED:
            assert path.startswith("gaming."), path
    assert profile["blocks"]["gaming_share"]["source"] == "assumed"
    assert profile["gaming"]["focus"]["game"]["source"] == MEASURED
    for part in ("desk", "shared", "events"):
        assert all(leaf["source"] == "assumed" for _, leaf in sc.leaves({part: profile[part]}))


def test_locks_are_a_count_in_the_profile(profile: dict[str, Any]) -> None:
    assert profile["events"]["per_day"]["lock"]["value"] == 2


def test_profile_without_source_is_refused_naming_the_key(profile: dict[str, Any]) -> None:
    bad = copy.deepcopy(profile)
    del bad["desk"]["run"]["median_s"]["source"]
    with pytest.raises(SystemExit, match=r"desk\.run\.median_s"):
        sc.validate_profile(bad)


@pytest.mark.parametrize("group", ["gaming.focus", "desk.monitor", "shared.language"])
def test_share_group_not_summing_to_one_is_refused(profile: dict[str, Any], group: str) -> None:
    bad = copy.deepcopy(profile)
    node = bad
    for part in group.split("."):
        node = node[part]
    node[next(iter(node))]["value"] += 0.01
    with pytest.raises(SystemExit, match=re.escape(group)):
        sc.validate_profile(bad)


def test_generator_refuses_a_profile_leaf_without_source(tmp_path: Path) -> None:
    text = (QUALITY / "profile.toml").read_text(encoding="utf-8")
    broken = text.replace(
        'median_s = { value = 30, source = "assumed" }', "median_s = { value = 30 }"
    )
    assert broken != text
    path = tmp_path / "profile.toml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(SystemExit) as err:
        sc.generate("smoke", 1, profile_path=path)
    assert err.value.code
    assert "median_s" in str(err.value.code)


# --- scenario.json -------------------------------------------------------------------------


def test_same_inputs_give_byte_identical_file() -> None:
    assert sc.generate("smoke", 7) == sc.generate("smoke", 7)


def test_another_seed_gives_another_timeline() -> None:
    assert scenario("standard", 1)["steps"] != scenario("standard", 2)["steps"]


def test_scenario_records_seed_length_and_hashes() -> None:
    scn = scenario("smoke")
    assert scn["seed"] == 5
    assert scn["length"] == "smoke"
    assert scn["hashes"] == {
        "profile": common.sha256_file(QUALITY / "profile.toml"),
        "coverage": common.sha256_file(QUALITY / "coverage.toml"),
        "manifest": common.sha256_file(QUALITY / "assets" / "manifest.json"),
    }


@pytest.mark.parametrize("length", ["smoke", "standard", "attended", "day"])
def test_steps_have_span_monitor_windows_and_known_tags(
    length: str, coverage: dict[str, Any]
) -> None:
    cases = {c["id"] for c in coverage["case"]}
    scn = scenario(length)
    assert scn["steps"]
    for s in scn["steps"]:
        assert s["t0_ms"] < s["t1_ms"]
        assert s["monitor"] in sc.MONITORS
        assert isinstance(s["windows"], list)
        assert set(s["tags"]) <= cases, s["id"]


def test_smoke_length_and_core_cases(coverage: dict[str, Any]) -> None:
    scn = scenario("smoke")
    assert 4.5 * 60_000 <= scn["duration_ms"] <= 5.5 * 60_000
    assert sc.cell_problems(scn["steps"], coverage, "smoke") == []
    tagged = {t for s in scn["steps"] for t in s["tags"]}
    assert {c["id"] for c in coverage["case"] if c.get("core")} <= tagged


def test_standard_is_one_gaming_then_one_desk_block_on_three_monitors() -> None:
    scn = scenario("standard")
    assert 55 * 60_000 <= scn["duration_ms"] <= 65 * 60_000
    assert [b["kind"] for b in scn["blocks"]] == ["gaming", "desk"]
    gaming = scn["blocks"][0]
    assert gaming["t1_ms"] - gaming["t0_ms"] == pytest.approx(0.35 * scn["duration_ms"], rel=0.02)
    assert {s["monitor"] for s in scn["steps"] if s["kind"] == "focus"} == set(sc.MONITORS)
    assert {w["monitor"] for w in scn["windows"]} == set(sc.MONITORS)


def test_day_is_six_to_eight_hours_of_alternating_blocks() -> None:
    scn = scenario("day")
    assert 6 * HOUR <= scn["duration_ms"] <= 8 * HOUR
    kinds = [b["kind"] for b in scn["blocks"]]
    assert len(kinds) >= 4
    assert all(a != b for a, b in itertools.pairwise(kinds))
    gaming = sum(b["t1_ms"] - b["t0_ms"] for b in scn["blocks"] if b["kind"] == "gaming")
    assert gaming / scn["duration_ms"] == pytest.approx(0.35, abs=0.03)


def test_attended_has_each_life_event_once_with_a_minute_of_activity_after() -> None:
    scn = scenario("attended")
    assert 10 * 60_000 <= scn["duration_ms"] <= 15 * 60_000
    kinds = Counter(s["kind"] for s in scn["steps"])
    for kind in ("lock", "sleep", "device_change", "elevated"):
        assert kinds[kind] == 1, kind
    events = sorted(
        (s for s in scn["steps"] if s["kind"] in ("lock", "sleep", "device_change", "elevated")),
        key=lambda s: s["t0_ms"],
    )
    assert sc.attended_gaps(scn["steps"], events) == []
    for ev, nxt in zip(events, [*events[1:], None], strict=True):
        end = nxt["t0_ms"] if nxt else scn["duration_ms"]
        active = sum(
            min(s["t1_ms"], end) - max(s["t0_ms"], ev["t1_ms"])
            for s in scn["steps"]
            if s["kind"] == "focus" and s["t0_ms"] < end and s["t1_ms"] > ev["t1_ms"]
        )
        assert active >= 60_000, ev["kind"]


# --- profile fidelity ----------------------------------------------------------------------


def block_measure(scn: dict[str, Any], kind: str) -> dict[str, Any]:
    fs = focus_steps(scn, kind)
    dur = sum(s["t1_ms"] - s["t0_ms"] for s in fs)
    hours = sum(b["t1_ms"] - b["t0_ms"] for b in scn["blocks"] if b["kind"] == kind) / HOUR
    focus: Counter[str] = Counter()
    mon: Counter[str] = Counter()
    for s in fs:
        length = s["t1_ms"] - s["t0_ms"]
        focus[sc.APP_CAT[s["params"]["app"]]] += length / dur
        mon[s["monitor"]] += length / dur
    spans = [(b["t0_ms"], b["t1_ms"]) for b in scn["blocks"] if b["kind"] == kind]
    titles = [
        s
        for s in scn["steps"]
        if s["kind"] == "title_change" and any(a <= s["t0_ms"] < b for a, b in spans)
    ]
    lens = [(s["t1_ms"] - s["t0_ms"]) / 1000 for s in fs]
    return {
        "focus": focus,
        "monitor": mon,
        "switches": len(fs) / hours,
        "median": statistics.median(lens),
        "p90": float(np.percentile(lens, 90)),
        "titles": len(titles) / hours,
        "title_ms": {s["t1_ms"] - s["t0_ms"] for s in titles},
    }


@pytest.mark.parametrize(("length", "pp", "rel"), [("standard", 5, 0.25), ("day", 3, 0.25)])
def test_profile_fidelity_of_focus_runs(
    length: str, pp: float, rel: float, profile: dict[str, Any]
) -> None:
    scn = scenario(length)
    for kind in ("gaming", "desk"):
        m = block_measure(scn, kind)
        want_mon = {k: v["value"] for k, v in profile[kind]["monitor"].items()}
        for mon, want in want_mon.items():
            assert abs(m["monitor"][mon] - want) * 100 <= pp, f"{kind} monitor {mon}"
        want_focus = {k: v["value"] for k, v in profile[kind]["focus"].items() if k != "video"}
        if kind == "desk":
            want_focus["browser"] += profile["desk"]["focus"]["video"]["value"]
        for cat, want in want_focus.items():
            assert abs(m["focus"][cat] - want) * 100 <= pp, f"{kind} focus {cat}"
        assert m["median"] == pytest.approx(profile[kind]["run"]["median_s"]["value"], rel=rel)
        assert m["p90"] == pytest.approx(scn["run_p90_s"][kind], rel=rel)
        assert m["title_ms"] == {2000}


@pytest.mark.parametrize("length", ["standard", "day"])
def test_profile_fidelity_of_switch_and_title_rates(length: str, profile: dict[str, Any]) -> None:
    scn = scenario(length)
    for kind in ("gaming", "desk"):
        m = block_measure(scn, kind)
        assert m["switches"] == pytest.approx(
            profile[kind]["run"]["switches_per_hour"]["value"], rel=0.15
        )
        assert m["titles"] == pytest.approx(
            profile[kind]["run"]["title_changes_per_hour"]["value"], rel=0.20
        )


def test_run_p90_is_recorded_and_scaled_per_length() -> None:
    assert scenario("standard")["run_p90_s"]["desk"] == 300
    assert scenario("day")["run_p90_s"]["desk"] == 600
    assert scenario("day")["run_p90_s"]["gaming"] == 160


@pytest.mark.parametrize(("length", "pp"), [("standard", 5), ("day", 5)])
def test_language_mix(length: str, pp: float, profile: dict[str, Any]) -> None:
    scn = scenario(length)
    ms: Counter[str] = Counter()
    for s in scn["steps"]:
        for u in s["params"].get("utterances", []):
            ms[scn["cast"][u["speaker"]]["lang"]] += u["dur_ms"]
    total = sum(ms.values())
    for lang, leaf in profile["shared"]["language"].items():
        assert abs(ms[lang] / total - leaf["value"]) * 100 <= pp, lang


def test_two_visible_windows_per_monitor() -> None:
    scn = scenario("standard")
    for s in scn["steps"]:
        if s["kind"] == "focus":
            on_monitor = Counter(w.split("@")[-1] for w in s["windows"] if "@" in w)
            assert max(on_monitor.values(), default=0) <= 2, s["id"]


@pytest.mark.parametrize("length", ["standard", "day"])
def test_call_speakers_come_back_and_videos_scale(length: str) -> None:
    scn = scenario(length)
    calls = [s for s in scn["steps"] if s["kind"] == "call"]
    assert calls
    assert all(2 <= len(c["params"]["cast"]) <= 4 for c in calls)
    seen = Counter(spk for c in calls for spk in set(c["params"]["cast"]))
    assert any(n >= 2 for n in seen.values())
    videos = [s for s in scn["steps"] if s["kind"] == "video"]
    episodes = [v for v in videos if v["params"]["episode"]]
    assert len(episodes) == 1
    assert "season" in episodes[0]["params"]
    assert "number" in episodes[0]["params"]
    if length == "day":
        assert 2 <= len(videos) <= 3


def test_unattended_events_scale_to_hours() -> None:
    day = Counter(s["kind"] for s in scenario("day")["steps"])
    assert (day["pause"], day["excluded_pages"], day["canary"], day["busy_disk"]) == (1, 3, 1, 1)
    std = Counter(s["kind"] for s in scenario("standard")["steps"])
    for kind in ("pause", "excluded_pages", "canary", "busy_disk"):
        assert std[kind] >= 1, kind


# --- coverage.toml -------------------------------------------------------------------------


def test_coverage_lists_13_dimensions_and_41_cases(coverage: dict[str, Any]) -> None:
    assert len(coverage["dimensions"]) == 13
    groups = Counter(c["group"] for c in coverage["case"])
    assert len(coverage["case"]) == 41
    assert (groups["apps"], groups["content"], groups["audio"], groups["life"]) == (13, 13, 8, 7)
    assert [c["id"] for c in coverage["case"] if c["id"].startswith("lock")] == ["lock"]


def test_attended_only_cells_are_impossible_for_unattended_lengths(
    coverage: dict[str, Any],
) -> None:
    entry = next(
        e for e in coverage["impossible"] if e["reason"] == "attended only (user answer 2026-11-18)"
    )
    assert entry["dims"] == "all"
    assert set(entry["cases"]) == {"lock", "sleep", "device_change", "elevated"}
    assert set(entry["lengths"]) == {"smoke", "standard", "day"}
    for length in ("smoke", "standard", "day"):
        cells = sc.impossible_cells(coverage, length)
        assert all((dim, case) in cells for dim in range(1, 14) for case in entry["cases"])
    assert all(c["reason"] for c in coverage["impossible"])


def test_export_is_the_one_deferred_location(coverage: dict[str, Any]) -> None:
    deferred = coverage["deferred_location"]
    assert [(d["metric"], d["location"]) for d in deferred] == [("M10.1", "export")]


@pytest.mark.parametrize("length", ["standard", "day"])
def test_standard_and_day_fill_every_unattended_cell(length: str, coverage: dict[str, Any]) -> None:
    assert sc.cell_problems(scenario(length)["steps"], coverage, length) == []


def test_attended_fills_its_cells(coverage: dict[str, Any]) -> None:
    assert sc.cell_problems(scenario("attended")["steps"], coverage, "attended") == []


def test_dimensions_10_and_13_count_only_under_their_own_step_tags(
    coverage: dict[str, Any],
) -> None:
    steps = [
        {"id": "a", "kind": "focus", "t0_ms": 0, "t1_ms": 100, "tags": ["chrome"], "dims": []},
        {"id": "b", "kind": "canary", "t0_ms": 0, "t1_ms": 100, "tags": ["canary"], "dims": [10]},
    ]
    counts = sc.cell_counts(steps, coverage)
    assert counts["10|canary"] == 1
    assert counts["10|chrome"] == 0
    assert counts["13|canary"] == 0


def test_generator_cell_check_reports_empty_and_impossible_cells(coverage: dict[str, Any]) -> None:
    assert any("is empty" in p for p in sc.cell_problems([], coverage, "standard"))
    steps = [{"id": "x", "kind": "lock", "t0_ms": 0, "t1_ms": 10, "tags": ["lock"], "dims": [5]}]
    assert "impossible cell 5|lock is filled" in sc.cell_problems(steps, coverage, "standard")


# --- change tags ---------------------------------------------------------------------------


def tag_frame(ids: list[int]) -> np.ndarray[Any, Any]:
    rng = np.random.default_rng(7)
    frame = np.full((2160, 3840, 3), 235, np.uint8)
    for y in range(40, 2100, 18):
        for _ in range(30):
            x0 = int(rng.integers(0, 3700))
            frame[y : y + 10, x0 : x0 + int(rng.integers(5, 140))] = int(rng.integers(0, 90))
    for k, tag_id in enumerate(ids):
        tagmod.paste(
            frame,
            tag_id,
            20 + (k % 20) * 190,
            20 + (k // 20) * 210,
            cell=int(rng.choice([8, 9, 10, 12])),
        )
    return frame


def test_tags_decode_from_lossless_and_webp_frames() -> None:
    rng = np.random.default_rng(3)
    ids = [int(i) for i in rng.integers(0, 1 << 24, 100)]
    frame = tag_frame(ids)
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, format="WEBP", quality=70, method=2)
    webp = np.asarray(Image.open(buf).convert("RGB"))
    for img in (frame, webp):
        got = set(tagmod.decode(img))
        assert sum(i in got for i in ids) / len(ids) >= 0.99


# --- assets --------------------------------------------------------------------------------


def test_committed_pack_matches_manifest_and_is_small() -> None:
    assert assets.verify() == 0
    size = sum(p.stat().st_size for p in (QUALITY / "assets").rglob("*") if p.is_file())
    assert size <= 50 * 1024 * 1024


def test_pack_mismatch_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.bin").write_bytes(b"x")
    manifest = {"files": {"a.bin": {"sha256": "0" * 64}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(assets, "ASSETS", tmp_path)
    assert assets.verify() == 1


# --- store and scorer ----------------------------------------------------------------------


@pytest.fixture
def harness_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "models-home"
    (home / "models").mkdir(parents=True)
    (home / "runtime").mkdir()
    monkeypatch.setattr(runmod, "MODELS_HOME", home)
    monkeypatch.setattr(scoring, "LOCAL", tmp_path / "local")
    monkeypatch.setattr(scoring, "RESULTS", tmp_path / "results")
    return tmp_path


def make_run(
    root: Path, run_id: str, *, fingerprint: str = "pc", length: str = "smoke", seed: int = 4
) -> Path:
    local = root / "local" / "runs" / run_id
    local.mkdir(parents=True)
    _, _, conn = runmod.make_store(local, 4321)
    conn.close()
    res = root / "results" / run_id
    (res / "truth").mkdir(parents=True)
    (res / "scenario.json").write_text(sc.generate(length, seed), encoding="utf-8", newline="\n")
    (res / "machine.json").write_text(
        json.dumps({"fingerprint": fingerprint, "monitors": []}), encoding="utf-8"
    )
    return res


def test_store_is_a_fresh_encrypted_folder_with_the_run_config(harness_dirs: Path) -> None:
    local = harness_dirs / "local" / "runs" / "r"
    local.mkdir(parents=True)
    paths, _key, conn = runmod.make_store(local, 4321)
    try:
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0
        assert paths.keyfile.is_file()
        wrapped = (local / "passphrase.dpapi").read_bytes()
        passphrase = common.dpapi_unwrap(wrapped).decode()
        assert len(passphrase.encode()) >= 32
        assert passphrase.encode() not in wrapped

        assert unwrap_data_key(paths.keyfile.read_bytes(), passphrase) is not None
        cfg = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
        assert cfg["audio"]["mic_device"] == "CABLE Output"
        assert cfg["exclusions"]["processes"] == ["qualityvault.exe"]
        assert cfg["exclusions"]["urls"] == ["127.0.0.1:4321/*"]
        assert passphrase not in paths.config_file.read_text(encoding="utf-8")
        assert paths.models_dir.is_junction()
        assert paths.runtime_dir.is_junction()
        assert (
            Path(paths.models_dir).resolve() == (harness_dirs / "models-home" / "models").resolve()
        )
    finally:
        conn.close()


def test_fold_and_edit_distances() -> None:
    assert scoring.fold("  Été,  Dîner!  ") == "été dîner"
    assert scoring.fold("Été", accents=False) == "ete"
    assert scoring.fold("é") == scoring.fold("é")
    assert scoring.lev("kitten", "sitting") == 3
    assert scoring.lev(["a", "b", "c"], ["a", "x", "c", "d"]) == 2


def test_diarization_assignment_is_optimal() -> None:
    conf = np.array([[10.0, 9.0], [9.0, 1.0]])
    assert scoring.best_assignment(conf) == {1: 0, 0: 1}


def test_scorer_outputs_hold_no_path_user_or_content(harness_dirs: Path) -> None:
    res = make_run(harness_dirs, "r1")
    scores = scoring.score("r1")
    assert scores["run_id"] == "r1"
    for name in ("scores.json", "coverage.json", "summary.md"):
        text = (res / name).read_text(encoding="utf-8")
        assert str(harness_dirs) not in text
        assert str(harness_dirs).replace("\\", "/") not in text
        assert getpass.getuser().lower() not in text.lower()
        assert common.CANARY_TEXT not in text


def test_scorer_reports_every_metric(harness_dirs: Path) -> None:
    make_run(harness_dirs, "r1")
    scores = scoring.score("r1")
    wanted = [
        f"M{m}.{i}"
        for m, n in (
            (1, 3),
            (2, 2),
            (3, 3),
            (4, 1),
            (5, 4),
            (6, 1),
            (7, 2),
            (8, 3),
            (9, 3),
            (10, 3),
            (11, 4),
            (12, 1),
            (13, 1),
        )
        for i in range(1, n + 1)
    ] + [f"G{i}" for i in range(1, 5)]
    assert [k for k in wanted if k not in scores] == []


def test_scorer_never_reports_zero_for_n_zero(harness_dirs: Path) -> None:
    make_run(harness_dirs, "r1")
    scores = scoring.score("r1")
    bad: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            d = cast("dict[str, object]", node)
            if d.get("n") == 0 and (
                d.get("value") is not None or "value" not in d or "reason" not in d
            ):
                bad.append(path)
            for key, val in d.items():
                walk(val, f"{path}.{key}")

    walk({k: v for k, v in scores.items() if k[0] in "MG"}, "")
    assert bad == []


def test_scorer_coverage_lists_missing_cells_and_exits_non_zero(
    harness_dirs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    res = make_run(harness_dirs, "r1")
    scoring.score("r1")
    cov = json.loads((res / "coverage.json").read_text(encoding="utf-8"))
    assert cov["missing"]
    assert scoring.coverage_cmd("r1") == 1
    assert cov["missing"][0] in capsys.readouterr().out


def test_coverage_command_passes_when_nothing_is_missing(harness_dirs: Path) -> None:
    res = harness_dirs / "results" / "ok"
    res.mkdir(parents=True)
    (res / "coverage.json").write_text(json.dumps({"missing": []}), encoding="utf-8")
    assert scoring.coverage_cmd("ok") == 0


# --- compare -------------------------------------------------------------------------------


def write_results(
    root: Path,
    name: str,
    scores: dict[str, Any],
    *,
    fingerprint: str = "pc",
    hashes: dict[str, str] | None = None,
) -> None:
    res = root / "results" / name
    res.mkdir(parents=True)
    (res / "machine.json").write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")
    (res / "scores.json").write_text(json.dumps(scores), encoding="utf-8")
    body: dict[str, Any] = {}
    body["hashes"] = hashes or {"profile": "p", "coverage": "c", "manifest": "m"}
    (res / "scenario.json").write_text(json.dumps(body), encoding="utf-8")


def test_compare_refuses_two_machines(harness_dirs: Path) -> None:
    write_results(harness_dirs, "a", {"M1.1": {"value": 1.0, "n": 5}}, fingerprint="one")
    write_results(harness_dirs, "b", {"M1.1": {"value": 1.0, "n": 5}}, fingerprint="two")
    assert scoring.compare("a", "b") != 0


def test_compare_lists_metrics_over_five_percent(
    harness_dirs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_results(
        harness_dirs,
        "a",
        {
            "M1.1": {"value": 0.90, "n": 5},
            "M2.1": {"value": 0.50, "n": 5},
            "M3.1": {"value": 0.0, "n": 5},
        },
    )
    write_results(
        harness_dirs,
        "b",
        {
            "M1.1": {"value": 0.90, "n": 5},
            "M2.1": {"value": 0.60, "n": 5},
            "M3.1": {"value": 0.0, "n": 5},
        },
    )
    assert scoring.compare("a", "b") == 1
    out = capsys.readouterr().out
    assert "M2.1.value" in out
    assert "M1.1.value" not in out
    assert "M3.1.value" not in out


def test_compare_within_five_percent_passes_and_flags_differing_hashes(
    harness_dirs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_results(harness_dirs, "a", {"M1.1": {"value": 1.00, "n": 5}})
    write_results(
        harness_dirs,
        "b",
        {"M1.1": {"value": 1.04, "n": 5}},
        hashes={"profile": "other", "coverage": "c", "manifest": "m"},
    )
    assert scoring.compare("a", "b") == 0
    out = capsys.readouterr().out
    assert "profile hash differs" in out
    assert "scenario differs" in out


def test_compare_two_zeros_are_equal(harness_dirs: Path) -> None:
    write_results(harness_dirs, "a", {"M1.1": {"value": 0, "n": 5}})
    write_results(harness_dirs, "b", {"M1.1": {"value": 0, "n": 5}})
    assert scoring.compare("a", "b") == 0


# --- repository settings -------------------------------------------------------------------


def test_repository_files_keep_results_exact_and_runs_local() -> None:
    root = QUALITY.parents[1]
    assert (
        "eval/quality/** -text"
        in (root / ".gitattributes").read_text(encoding="utf-8").splitlines()
    )
    assert "/eval/quality/local/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
