# -*- coding: utf-8 -*-
"""مسار بديل للكتابة: Gemini في متصفح المستخدم بدل واجهة DeepSeek (عند توقفها أو نفاد رصيدها).

الفكرة: نستعمل خط الإنتاج نفسه (البوابات والحقائق والنشر)، ونستبدل «مكالمة النموذج» بملفات:
  - لا إجابة جاهزة  → يُكتب طلب في elcinema_data/browser/requests ويُجمَع في browser/pending.json،
                      ويبقى العمل «محجوزًا» (browser-pending) ولا يُنشر شيء.
  - المهمة المجدولة في المتصفح تقرأ pending.json، تسأل Gemini، وتُودع حزمة إجابات واحدة في browser/answers/.
  - التشغيل التالي يقرأ الإجابة ويمرّرها عبر البوابات نفسها (طول، نسخ، عبارات جاهزة، افتتاحية مكرّرة...).
الاستعمال: python elcinema_browser.py publish <نفس وسائط elcinema_publish.py run>
           python elcinema_browser.py recaps  <نفس وسائط elcinema_recaps.py>
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ["LLM_PROVIDER"] = "deepseek"                 # يجعل llm_config صالحًا؛ لا اتصال بالشبكة في هذا المسار
if not os.environ.get("DEEPSEEK_API_KEY"):
    os.environ["DEEPSEEK_API_KEY"] = "browser-mode"

import elcinema_publish as P  # noqa: E402
import elcinema_recaps as R   # noqa: E402

BASE = Path(os.environ.get("BROWSER_DIR", "elcinema_data/browser"))
REQ, ANS = BASE / "requests", BASE / "answers"
MAX_REQ = int(os.environ.get("BROWSER_MAX_REQ", "20"))
KEEP_DAYS = 14
_new = 0
_answers = None


class Pending(Exception):
    pass


def _key(prefix: str, text: str) -> str:
    return prefix + "-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:14]


def _recap_id(title: str, ep: int) -> str:
    return _key("rc", f"{title}|{ep}")


def _load_answers() -> dict:
    """يدمج كل ملفات الإجابات: إمّا حزمة {"answers": {id: {...}}} أو إجابة مفردة {"id":..., ...}."""
    global _answers
    if _answers is None:
        _answers = {}
        for p in sorted(ANS.glob("*.json")) if ANS.exists() else []:
            try:
                d = json.loads(p.read_text("utf-8"))
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("answers"), dict):
                for k, v in d["answers"].items():
                    if isinstance(v, dict):
                        _answers[k] = v
            elif isinstance(d, dict) and d.get("id"):
                _answers[d["id"]] = d
    return _answers


def _answer(rid: str):
    return _load_answers().get(rid)


def _ask(rid: str, req: dict) -> None:
    global _new
    p = REQ / (rid + ".json")
    if p.exists() or _new >= MAX_REQ:
        return
    REQ.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(req, id=rid), ensure_ascii=False, indent=1) + "\n", "utf-8")
    _new += 1


def browser_llm(system, user, model=None, key=None, session=None):
    m = re.match(r"المسلسل: (.+?) — الحلقة (\d+)\n", user)
    if m and system == R.WRITE_SYS:                       # ملخص حلقة: النص يأتي مع إجابة البحث نفسها
        a = _answer(_recap_id(m.group(1), int(m.group(2))))
        art = a.get("article") if a else None
        return json.dumps(art, ensure_ascii=False) if isinstance(art, dict) else ""
    rid = _key("wk", system + "\n" + user)
    a = _answer(rid)
    if a and isinstance(a.get("text"), str) and a["text"].strip():
        return a["text"]
    _ask(rid, {"kind": "work", "system": system, "user": user})
    raise Pending(rid)


def browser_research(title, year, ep, summary, key=None, session=None):
    rid = _recap_id(title, ep)
    a = _answer(rid)
    if not a:
        _ask(rid, {"kind": "recap", "title": title, "year": year, "ep": ep, "summary": (summary or "")[:800],
                   "research_sys": R.RESEARCH_SYS, "write_sys": R.WRITE_SYS})
        return {"events": [], "watch": None, "domains": set(), "cited": ""}
    seen = {R.domain(str(x)) for x in (a.get("sources_seen") or [])}
    seen.discard("")
    ev = a.get("events")
    return {"events": ev if isinstance(ev, list) else [], "watch": a.get("watch"), "domains": seen, "cited": ""}


_orig_generate = P.generate


def _generate(facts, source, args, llm):
    try:
        return _orig_generate(facts, source, args, llm)
    except Pending:
        return None, "browser-pending"


P.generate = _generate
P.run.__defaults__ = (browser_llm, None)
R.run.__defaults__ = (browser_llm, browser_research, None)


def _finish() -> None:
    """يجمّع الطلبات التي لا إجابة لها في pending.json، ويحذف الملفات القديمة."""
    BASE.mkdir(parents=True, exist_ok=True)
    pend = []
    for p in sorted(REQ.glob("*.json")) if REQ.exists() else []:
        try:
            d = json.loads(p.read_text("utf-8"))
        except ValueError:
            continue
        if d.get("id") not in _load_answers():
            pend.append(d)
    (BASE / "pending.json").write_text(json.dumps({"updated_at": P.now_iso(), "count": len(pend), "requests": pend},
                                                  ensure_ascii=False, indent=1) + "\n", "utf-8")
    cut = time.time() - KEEP_DAYS * 86400
    for d in (REQ, ANS):
        for p in d.glob("*.json") if d.exists() else []:
            if p.stat().st_mtime < cut:
                p.unlink()
    print(f"طلبات معلّقة للمتصفح: {len(pend)} (جديدة في هذه الدورة: {_new})")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("publish", "recaps"):
        raise SystemExit("الاستعمال: elcinema_browser.py publish|recaps [وسائط]")
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "publish":
            rc = P.main(["run"] + rest + ["--retry-held"])
        else:
            rc = R.main(rest + ["--retry-held"])
    finally:
        _finish()
    return rc


if __name__ == "__main__":
    sys.exit(main())
