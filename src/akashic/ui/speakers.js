"use strict";
// Speakers: the voices Akashic has told apart. Name them once, merge two that are one person,
// or move a turn that was given to the wrong voice. Uses the helpers of app.js.

const fmtSeconds = (ms) => {
  const s = Math.round(ms / 1000);
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
};

const voiceName = (s) => s.name || (s.is_self ? "You" : `Unknown voice ${s.id}`);

// One clip at a time: a turn is played from its start offset inside the segment's recording.
const clip = { audio: null, btn: null };
function stopClip() {
  if (clip.audio) clip.audio.pause();
  if (clip.btn) setPlaying(clip.btn, false);
  clip.audio = null;
  clip.btn = null;
}
function playClip(btn, segmentId, startMs, endMs) {
  const same = clip.btn === btn;
  stopClip();
  if (same) return;
  const audio = new Audio(`/api/audio/${segmentId}`);
  clip.audio = audio;
  clip.btn = btn;
  audio.addEventListener("loadedmetadata", () => { audio.currentTime = startMs / 1000; }, { once: true });
  audio.addEventListener("timeupdate", () => { if (audio.currentTime * 1000 >= endMs) stopClip(); });
  audio.addEventListener("ended", stopClip);
  setPlaying(btn, true);
  audio.play().catch(() => stopClip());
}

function speakersView() {
  setActive("speakers");
  const list = h("div", { class: "speakers" }, h("div", { class: "muted" }, "Loading…"));
  view.replaceChildren(h("div", { class: "column" },
    pageHead("Speakers"),
    h("p", { class: "page-sub" }, "Voices heard in system sound, such as calls. Name each one once; the name is remembered for next time."),
    list));
  cleanup = stopClip;

  async function load() {
    let speakers;
    try { speakers = await getJson("/api/speakers"); } catch {
      list.replaceChildren(h("div", { class: "empty" }, "Cannot read the speakers."));
      return;
    }
    if (!speakers.length) {
      list.replaceChildren(h("div", { class: "empty" }, "No voices yet. They appear once transcripts have been processed."));
      return;
    }
    list.replaceChildren(...speakers.map((s) => card(s, speakers)));
  }

  function card(s, all) {
    const label = voiceName(s);
    const name = h("input", {
      type: "text", value: s.name || "", placeholder: s.is_self ? "You" : "Name this voice",
      maxlength: 80, "aria-label": `Name of ${label}`,
    });
    name.addEventListener("change", async () => {
      await sendJson(`/api/speakers/${s.id}/name`, { name: name.value }).catch(() => {});
      load();
    });
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); });

    const play = playButton("Play a sample");
    if (s.sample) play.addEventListener("click", () => playClip(play, s.sample.segment_id, s.sample.start_ms, s.sample.end_ms));
    else play.disabled = true;

    const others = all.filter((o) => o.id !== s.id);
    const merge = h("select", { "aria-label": `Merge ${label} into…` },
      h("option", { value: "" }, "Merge into…"),
      ...others.map((o) => h("option", { value: o.id }, voiceName(o))));
    merge.hidden = s.is_self || !others.length;
    merge.addEventListener("change", async () => {
      if (!merge.value) return;
      if (!confirm(`Merge ${label} into ${merge.selectedOptions[0].textContent}? They become one voice.`)) {
        merge.value = "";
        return;
      }
      await sendJson(`/api/speakers/${s.id}/merge`, { into: Number(merge.value) }).catch(() => {});
      load();
    });

    const turnsBox = h("div", { class: "turns", hidden: true });
    const toggle = h("button", { type: "button", class: "ghost", "aria-expanded": "false" }, "Show recent turns");
    toggle.addEventListener("click", async () => {
      turnsBox.hidden = !turnsBox.hidden;
      toggle.textContent = turnsBox.hidden ? "Show recent turns" : "Hide turns";
      toggle.setAttribute("aria-expanded", String(!turnsBox.hidden));
      if (!turnsBox.hidden) turnsBox.replaceChildren(...(await recentTurns(s, others, load)));
    });

    const meta = [`${s.turns} ${s.turns === 1 ? "turn" : "turns"}`, fmtSeconds(s.speech_ms) + " of speech"];
    if (s.last_ms) meta.push("last " + fmtTime(s.last_ms));
    return h("section", { class: "speaker-card" },
      h("div", { class: "speaker-head" }, play,
        h("div", { class: "speaker-main" }, name, h("div", { class: "muted small" }, meta.join(" · "))),
        merge.hidden ? null : helpIcon("Merge this voice into another when both are the same person. The destination keeps its name; past and future matches are combined."),
        merge, s.turns ? toggle : null),
      turnsBox);
  }

  load();
}

async function recentTurns(s, others, reload) {
  let turns;
  try { turns = await getJson(`/api/speakers/${s.id}/turns?limit=20`); } catch { return [h("div", { class: "empty" }, "Cannot read the turns.")]; }
  return turns.map((t) => {
    const play = playButton("Play", "small-play");
    play.addEventListener("click", () => playClip(play, t.segment_id, t.start_ms, t.end_ms));
    const move = h("select", { "aria-label": "Not this voice: move to…" },
      h("option", { value: "" }, "Not this voice…"),
      h("option", { value: "new" }, "A new voice"),
      ...others.map((o) => h("option", { value: o.id }, voiceName(o))));
    move.addEventListener("change", async () => {
      if (!move.value) return;
      const speaker_id = move.value === "new" ? null : Number(move.value);
      await sendJson(`/api/turns/${t.id}/assign`, { speaker_id }).catch(() => {});
      reload();
    });
    return h("div", { class: "row" }, play,
      h("div", { class: "row-body" },
        metaLine(fmtTime(t.at_ms), fmtSeconds(t.speech_ms)),
        h("div", { class: "row-snippet" }, t.text || "Listen to the clip; its words are not assigned to this voice.")),
      move);
  });
}
