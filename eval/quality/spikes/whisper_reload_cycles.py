"""Does loading and unloading Whisper again and again in one process leak VRAM or start failing?

The day run's worker loaded Whisper 134 times in about 2 h (it is dropped whenever an embed job
runs), then one load raised ValueError and every later load failed the same way. This spike does
the same in one process, as `pipeline/whisper.py` does it (`WhisperModel(dir, device="cuda",
compute_type="float16")`, then the reference dropped): load, transcribe 3 s of quiet synthetic
noise, unload, and sample this process's dedicated VRAM (PDH, as the harness's G2) after each
cycle. Stops at the first exception and records its full message. No window, no captured data.

    uv run python eval/quality/spikes/whisper_reload_cycles.py <out.json> [cycles] [--gc]
"""

import gc
import json
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import winapi  # noqa: E402

os.environ.setdefault("HF_HUB_OFFLINE", "1")
from akashic.pipeline.whisper import add_cuda_dll_directories  # noqa: E402

MODEL = Path(__file__).resolve().parents[1] / "local" / "models-home" / "models" / "faster-whisper-large-v3"


def main() -> None:
    out = Path(sys.argv[1])
    cycles = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 160
    collect = "--gc" in sys.argv
    add_cuda_dll_directories()
    import faster_whisper as fw

    gpu = winapi.GpuCounters()
    pid = os.getpid()
    vram = lambda: round(gpu.sample()[0].get(pid, 0.0) / 2**20)
    audio = (np.random.default_rng(0).standard_normal(16000 * 3) * 0.01).astype(np.float32)
    rows, error = [], None
    base = vram()
    for i in range(cycles):
        t0 = time.perf_counter()
        try:
            model = fw.WhisperModel(str(MODEL), device="cuda", compute_type="float16", local_files_only=True)
            loaded = vram()
            segs, _ = model.transcribe(audio, language="en", beam_size=5, vad_filter=False)
            list(segs)
            model = None
            if collect:
                gc.collect()
        except Exception as exc:  # noqa: BLE001 - the point is the message
            error = {"cycle": i, "type": type(exc).__name__, "message": str(exc), "trace": traceback.format_exc()[-1500:]}
            break
        rows.append({"cycle": i, "s": round(time.perf_counter() - t0, 2), "loaded_mib": loaded, "after_mib": vram()})
        if i % 10 == 0:
            print(rows[-1], flush=True)
    result = {"cycles_done": len(rows), "gc_collect": collect, "base_mib": base, "error": error, "rows": rows}
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print("done", len(rows), "error", error and error["type"], error and error["message"])
    if rows:
        print("after_mib first/last", rows[0]["after_mib"], rows[-1]["after_mib"], "loaded first/last", rows[0]["loaded_mib"], rows[-1]["loaded_mib"])


if __name__ == "__main__":
    main()
