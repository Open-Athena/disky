"""The digest's Slack wire goldens (specs/slack-in-workers.md): what thrds's `SlackClient.post` / `.edit` send for each
digest body (every OP in `fixtures/digest/parity/`, every reply in `fixtures/digest/replies/`, and a few edge shapes),
captured from its requests with the image cache-bust token fixed, in `fixtures/digest/wire/payloads.json`
(`DT_UPDATE_GOLDEN=1` rewrites it). `site/functions/_lib/digestWire.test.ts` must build the same payloads, so a
Worker's converge posts what the job does."""
from __future__ import annotations

import json
import os
from pathlib import Path

import thrds.slack as TS
from thrds.slack import SlackClient

FIX = Path(__file__).parent / "fixtures" / "digest"
GOLDEN = FIX / "wire" / "payloads.json"
BUST = "29747712"
#: A plot line that asks for a fresh cache-bust token (`{bust}`), a lone image line mid-body (stays text), an emoji
#: image (`![:name:](…)`, collapsed to `:name:`), a long body over image blocks (split into sections at its blank
#: lines), and a struck-through alert.
EXTRA = {
    "bust-image": "**R2 — November**\nline *one* · [link](https://x.example/a?b=1)\n\n![plot](https://p.example/plot.png?v=3){bust}",
    "mid-image": "head\n![not lifted](https://p.example/a.png)\ntail",
    "emoji-image": "a ![:arrow_deg30:](arrow_deg30.png) b",
    "long-over-image": "\n\n".join(f"para {i} " + "x" * 600 for i in range(8)) + "\n\n![plot](https://p.example/p.png)",
    "struck-alert": "~:warning: quota over~\n~line two~\nresolved 11/3",
}


def bodies() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted((FIX / "parity").glob("*.json")):
        out[f"op:{p.stem}"] = json.loads(p.read_text())["op"]
    for p in sorted((FIX / "replies").glob("*.json")):
        for r in json.loads(p.read_text())["replies"]:
            out[f"reply:{p.stem}:{r['key']}"] = r["body"]
    return {**out, **{f"extra:{k}": v for k, v in EXTRA.items()}}


def capture(monkeypatch) -> dict:
    sent: list[tuple[str, dict]] = []
    client = SlackClient("xoxb-test", "C0TEST")
    monkeypatch.setattr(client, "_request", lambda method, data, **kw: sent.append((method, json.loads(json.dumps(data)))) or {"ts": "1.000001"})
    monkeypatch.setattr(TS, "bust_token", lambda: BUST)
    out = {}
    for name, body in bodies().items():
        sent.clear()
        client.post(body, thread_id="1.000000", username="wk of 11/2", icon_url="https://i.example/mark.png")
        client.edit("1.000002", body)
        (pm, post), (em, edit) = sent
        assert (pm, em) == ("chat.postMessage", "chat.update")
        out[name] = {"body": body, "post": post, "edit": edit}
    return {"bust": BUST, "cases": out}


def test_wire_goldens(monkeypatch):
    got = capture(monkeypatch)
    if os.environ.get("DT_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n")
    assert json.loads(GOLDEN.read_text()) == got
