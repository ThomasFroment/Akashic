"""Cross-tab of the M3.1 rows by resolution x source x font bucket x theme (numbers only).

Re-runs score.m3's row loop on a finished run without changing score.py, and reports mean CER and n
per cell, plus how often the stored text has no line close to a shown line (CER >= 0.9).

    uv run python eval/small-text-ocr/crosstab_m31.py <run-id> [<run-id> ...]
"""

import bisect
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "eval" / "quality"))
import score  # noqa: E402


def rows_of(rid: str):
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    lines = {}
    for e in tr.pages:
        if e.get("type") == "lines" and "#private" not in e["w"]:
            for ln in e["lines"]:
                lines[ln["id"]] = {**ln, "w": e["w"], "s": e["s"]}
    visible = score.visible_reports(tr)
    report_times = {wid: [t for t, _ in reports] for wid, reports in visible.items()}
    focus_params = {s["id"]: s["params"] for s in tr.scn["steps"] if s["kind"] == "focus"}
    for d in tr.driver:
        if d["action"] == "page_load" and d.get("url"):
            q = {k: v[0] for k, v in parse_qs(urlsplit(d["url"]).query).items()}
            if "s" in q:
                focus_params[q["s"]] = {"lang": q.get("lang"), "theme": q.get("theme"), "font_px": int(q.get("px", 16))}
    rows = conn.execute("SELECT w.hwnd, f.ts_ms, w.text, w.text_source, w.x1 - w.x0, w.y1 - w.y0 FROM frame_windows w JOIN frames f ON f.id = w.frame_id"
                        " WHERE w.role = 'app' AND w.text IS NOT NULL ORDER BY f.ts_ms").fetchall()
    sample = rows[:: max(1, len(rows) // score.MAX_TEXT_ROWS)] if rows else []
    for h, ts, text, src, ww, wh in sample:
        wid = tr.hwnd_to_window.get(h)
        i = bisect.bisect_right(report_times.get(wid, []), ts) - 1
        shown = [lines[x] for x in visible[wid][i][1] if x in lines] if i >= 0 else []
        if not shown:
            continue
        hyp = [score.fold(x) for x in text.splitlines() if x.strip()]
        params = focus_params.get(shown[-1]["s"], {})
        mon = score.monitor_of(tr, wid)
        cers = [score.best_line_cer(ln["text"], hyp) for ln in shown]
        device_px = params.get("font_px", 16) * mon.get("scale", 1)
        bucket = next((b[2] for b in score.FONT_BUCKETS if b[0] <= device_px < b[1] + 1), "?")
        app = (wid or "?").split("@")[0].split("-")[0]
        yield {"res": f"{mon.get('w', '?')}", "src": src, "font": bucket, "theme": params.get("theme", "?"), "app": app,
               "cer": statistics.mean(cers), "miss": sum(c >= 0.9 for c in cers) / len(cers), "n_shown": len(shown),
               "n_hyp": len(hyp), "win_w": ww}


def main() -> None:
    cells = defaultdict(list)
    for rid in sys.argv[1:]:
        for r in rows_of(rid):
            for key in [(r["res"], r["src"], r["font"]), (r["res"], r["src"], r["font"], r["theme"]), (r["res"], r["src"], r["app"])]:
                cells[key].append(r)
    print("cell | n | mean CER | share of shown lines missed (CER>=0.9) | median CER")
    for key in sorted(cells, key=lambda k: (len(k), k)):
        rs = cells[key]
        if len(rs) < 5:
            continue
        print(" ".join(map(str, key)), "|", len(rs), "|", round(statistics.mean(r["cer"] for r in rs), 3), "|",
              round(statistics.mean(r["miss"] for r in rs), 3), "|", round(statistics.median(r["cer"] for r in rs), 3))


if __name__ == "__main__":
    main()
