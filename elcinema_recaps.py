#!/usr/bin/env python3
"""
elcinema_recaps.py — ملخصات الحلقات: elcinema (المصدر الموثوق) + بحث Claude في الويب (للتحقق وإكمال الأحداث)
    حلقة من قاعدة elcinema → بحث ويب لاكتشاف الأحداث → اعتماد ما أكّده مصدران مستقلان فقط
    → كتابة مقال بأسلوب/بنية تتبدّل لكل حلقة → بوابة عدم‑النسخ والتنويع → نشر (نفس أهداف elcinema_publish)

القاعدة: ما لا تؤكده مصادر كافية لا يُكتب. تُحجز الحلقة بدل اختلاق أحداثها.

المتغيرات: ANTHROPIC_API_KEY، ANTHROPIC_MODEL، WEB_SEARCH_TOOL (افتراضي web_search_20250305)
          + متغيرات الهدف نفسها في elcinema_publish.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests

import elcinema_publish as P

RECAP_DDL = """
CREATE TABLE IF NOT EXISTS recap_items (
  work_id INTEGER, episode_no INTEGER, state TEXT, facts_hash TEXT, article TEXT, remote_id TEXT,
  remote_url TEXT, notes TEXT, updated_at TEXT, published_hash TEXT, opening TEXT,
  PRIMARY KEY (work_id, episode_no));
"""
CLICHES = ("في هذه الحلقة", "تدور أحداث", "شهدت الحلقة", "في الحلقة الجديدة", "وسط أجواء", "تتصاعد الأحداث",
           "في حلقة اليوم", "ملخص الحلقة")
# أنماط كتابة تتبدّل حسب العمل والحلقة؛ الهدف تنويع حقيقي في البنية لا تمويه
STYLES = [
    "ابدأ بأقوى لحظة في الحلقة ثم ارجع لترتيب ما قاد إليها.",
    "اكتب بسرد زمني متتابع هادئ، فقرة لكل مرحلة من الحلقة.",
    "اجعل محور المقال شخصية واحدة وكيف تغيّر موقفها خلال الحلقة.",
    "ابدأ بسؤال يشغل المشاهد بعد الحلقة، ثم أجب عنه عبر الأحداث.",
    "قارن بين حال الشخصيات في أول الحلقة وآخرها.",
    "اكتب بنبرة محلل درامي يربط الأحداث بما قبلها دون خروج عن الحقائق.",
    "ابدأ بالخلاصة في جملتين ثم فصّل.",
    "اعرض الأحداث كمحاور قصيرة بعناوين فرعية مدمجة في النص.",
]


def pick(seed: str, n: int) -> int:
    return int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16) % n


def domain(u: str) -> str:
    h = (urlparse(u).netloc or u).lower()
    return h[4:] if h.startswith("www.") else h


# ───────────────────────── البحث في الويب ─────────────────────────
def api_post(body: dict, key: str, session=requests) -> dict:
    last = None
    for i in range(4):
        try:
            r = session.post("https://api.anthropic.com/v1/messages", timeout=180, json=body,
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            if r.status_code in (429, 500, 502, 503, 529):
                last = f"http {r.status_code}"
                time.sleep(2 ** i * 3)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 ** i * 3)
    raise RuntimeError(f"API failed: {last}")


RESEARCH_SYS = """أنت باحث دراما. ابحث في الويب العربي عن أحداث حلقة محددة من مسلسل، واستخرج حقائق الحلقة فقط.
أعد JSON فقط: {"events":[{"fact":"حدث واحد محدد بجملة قصيرة بصياغتك","sources":["نطاق الموقع","نطاق آخر"]}]}
- لا تنقل جملًا حرفية. لا تضف حدثًا لم تجده في نتائج البحث. اذكر نطاقات المواقع التي وجدت فيها الحدث فعلًا."""


def research(title: str, year, ep: int, summary: str, key: str, session=requests) -> dict:
    """يعيد {events:[{fact,sources}], domains:set, cited:str}"""
    user = f"المسلسل: {title}" + (f" ({year})" if year else "") + f"\nالحلقة رقم: {ep}\n"
    if summary:
        user += f"ملخص رسمي قصير للمساعدة في التعرّف على الحلقة:\n{summary[:800]}\n"
    body = {"model": os.environ.get("ANTHROPIC_MODEL", P.DEFAULT_MODEL), "max_tokens": 3000, "system": RESEARCH_SYS,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"type": os.environ.get("WEB_SEARCH_TOOL", "web_search_20250305"), "name": "web_search", "max_uses": 4}]}
    j = api_post(body, key, session)
    texts, domains, cited = [], set(), []
    for b in j.get("content", []):
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
            for c in b.get("citations") or []:
                if c.get("url"):
                    domains.add(domain(c["url"]))
                if c.get("cited_text"):
                    cited.append(c["cited_text"])
        elif b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            for r in b["content"]:
                if r.get("url"):
                    domains.add(domain(r["url"]))
    raw = "".join(texts)
    a, z = raw.find("{"), raw.rfind("}")
    events = []
    if a >= 0 and z > a:
        try:
            events = json.loads(raw[a:z + 1]).get("events", [])
        except json.JSONDecodeError:
            events = []
    return {"events": events, "domains": domains, "cited": " ".join(cited)}


def verified_events(res: dict, min_sources: int) -> list[str]:
    """يُعتمد الحدث فقط إن أكّدته ≥ min_sources نطاقات مختلفة موجودة فعلًا في نتائج البحث."""
    out = []
    for ev in res["events"]:
        if not isinstance(ev, dict) or not str(ev.get("fact", "")).strip():
            continue
        doms = {domain(str(s)) for s in (ev.get("sources") or [])}
        doms = {d for d in doms if d in res["domains"] and "elcinema" not in d}
        if len(doms) >= min_sources:
            out.append(str(ev["fact"]).strip())
    return out


# ───────────────────────── الكتابة والبوابة ─────────────────────────
WRITE_SYS = """أنت محرر دراما عربي. تكتب ملخص حلقة لقارئ يريد أن يفهم ما جرى، بصوت تحريري طبيعي ومتنوع.
- اعتمد على «الأحداث المؤكدة» و«الملخص الرسمي» فقط. لا تضف حدثًا أو اسمًا أو سببًا غير مذكور. إن كانت الأحداث قليلة فاكتب أقصر ولا تحشُ.
- صغ بكلماتك وببنية مختلفة؛ ممنوع النسخ من أي مصدر. تجنّب العبارات الجاهزة الشائعة مثل «في هذه الحلقة» و«تدور أحداث» و«شهدت الحلقة».
- لا تذكر أسماء مواقع ولا تعليقات ولا تقييمات.
أعد JSON فقط: headline، intro (30-60 كلمة)، overview (فقرات مفصولة بسطر فارغ بالطول المطلوب)، highlights (قائمة 2-4 نقاط)، faq (قائمة {q,a} من 0 إلى 2 من الأحداث فقط)."""


def cliche_count(txt: str) -> int:
    return sum(txt.count(c) for c in CLICHES)


def write_recap(facts: dict, ep: int, summary: str, events: list[str], llm, key: str, model: str) -> dict | None:
    seed = f"{facts['work_id']}-{ep}"
    style = STYLES[pick(seed, len(STYLES))]
    target = 150 + pick(seed + "L", 6) * 30            # 150..300 كلمة
    user = (f"المسلسل: {facts['title']} — الحلقة {ep}\nنمط الكتابة المطلوب لهذه الحلقة: {style}\n"
            f"الطول المطلوب لنص overview: نحو {target} كلمة (أقل إن لم تكفِ الأحداث).\n\n"
            f"الملخص الرسمي:\n{summary or '(لا يوجد)'}\n\nالأحداث المؤكدة:\n" + "\n".join(f"- {e}" for e in events))
    return P.parse_article(llm(WRITE_SYS, user, model, key))


def recap_gate(a: dict, source: str, facts: dict, min_words: int, prev_openings: set[str]) -> list[str]:
    why = P.gate(a, source, facts, min_words)
    txt = P.article_text(a)
    if cliche_count(txt) > 1:
        why.append("عبارات جاهزة متكررة")
    op = " ".join(P.words(a["intro"])[:6])
    if op and op in prev_openings:
        why.append("افتتاحية مطابقة لمقال سابق")
    return why


# ───────────────────────── التشغيل ─────────────────────────
def candidates(db, min_year: int, max_year: int):
    q = ("SELECT e.work_id, e.episode_no, COALESCE(e.summary,'') summary FROM episodes e JOIN works w ON w.work_id=e.work_id "
         "WHERE w.year>=? " + ("AND w.year<=? " if max_year else "") + "ORDER BY w.first_seen_at DESC, e.work_id, e.episode_no DESC")
    return db.execute(q, (min_year, max_year) if max_year else (min_year,)).fetchall()


def run(args, llm=P.call_llm, research_fn=research, session=None) -> dict:
    session = session or requests.Session()
    db = P.open_db(args.data_dir)
    db.executescript(RECAP_DDL)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY غير مضبوط")
    model = os.environ.get("ANTHROPIC_MODEL", P.DEFAULT_MODEL)
    st = dict(researched=0, published=0, held=0, errors=0, skipped=0)
    openings = {r[0] for r in db.execute("SELECT opening FROM recap_items WHERE opening IS NOT NULL")}
    for row in candidates(db, args.min_year, args.max_year):
        if st["researched"] >= args.limit and st["published"] >= args.limit:
            break
        wid, ep, summary = row["work_id"], row["episode_no"], row["summary"]
        facts, _ = P.build_facts(db, wid, args.min_year)
        facts.update(episode_no=ep, schema_type="TVEpisode", spoiler=True)
        parent = db.execute("SELECT remote_url FROM site_items WHERE work_id=?", (wid,)).fetchone()
        facts["related"] = [{"title": facts["title"], "url": parent["remote_url"] if parent else None}]
        h = hashlib.sha1(json.dumps([facts["title"], ep, summary], ensure_ascii=False).encode()).hexdigest()
        old = db.execute("SELECT * FROM recap_items WHERE work_id=? AND episode_no=?", (wid, ep)).fetchone()
        if old and old["state"] == "published" and old["published_hash"] == h + "|" + args.status:
            st["skipped"] += 1
            continue
        if old and old["state"] == "held" and old["facts_hash"] == h and not args.retry_held:
            st["skipped"] += 1
            continue
        art = json.loads(old["article"]) if old and old["article"] and old["facts_hash"] == h and old["state"] in ("ready", "error") else None
        if art is None:
            if st["researched"] >= args.limit:
                continue
            st["researched"] += 1
            res = research_fn(facts["title"], facts.get("year"), ep, summary, key, session)
            events = verified_events(res, args.min_sources)
            notes = ""
            if len(events) < args.min_events and len(summary) < 120:
                notes = f"مصادر غير كافية ({len(events)} حدث مؤكد)"
            else:
                source = summary + " " + res["cited"]
                for _ in range(2):
                    a = write_recap(facts, ep, summary, events, llm, key, model)
                    if a is None:
                        notes = "مخرجات غير صالحة"
                        continue
                    why = recap_gate(a, source, facts, args.min_words, openings)
                    if not why:
                        art = a
                        break
                    notes = "؛ ".join(why)
            if art is None:
                db.execute("""INSERT INTO recap_items(work_id,episode_no,state,facts_hash,notes,updated_at) VALUES(?,?,?,?,?,?)
                              ON CONFLICT(work_id,episode_no) DO UPDATE SET state='held',facts_hash=excluded.facts_hash,
                              notes=excluded.notes,updated_at=excluded.updated_at""", (wid, ep, "held", h, notes, P.now_iso()))
                db.commit()
                st["held"] += 1
                print(f"✗ {facts['title']} ح{ep} محجوزة: {notes}")
                continue
            op = " ".join(P.words(art["intro"])[:6])
            openings.add(op)
            db.execute("""INSERT INTO recap_items(work_id,episode_no,state,facts_hash,article,opening,notes,updated_at) VALUES(?,?,?,?,?,?,?,?)
                          ON CONFLICT(work_id,episode_no) DO UPDATE SET state='ready',facts_hash=excluded.facts_hash,
                          article=excluded.article,opening=excluded.opening,notes=excluded.notes,updated_at=excluded.updated_at""",
                       (wid, ep, "ready", h, json.dumps(art, ensure_ascii=False), op, f"{len(events)} أحداث مؤكدة", P.now_iso()))
            db.commit()
        if args.no_publish or st["published"] >= args.limit:
            continue
        try:
            rid, url = P.publish(args, session, wid * 1000 + ep, art, facts, old["remote_id"] if old else None)
        except Exception as ex:                    # noqa: BLE001
            db.execute("UPDATE recap_items SET state='error',notes=?,updated_at=? WHERE work_id=? AND episode_no=?",
                       (str(ex)[:300], P.now_iso(), wid, ep))
            db.commit()
            st["errors"] += 1
            print(f"! {facts['title']} ح{ep} فشل النشر: {ex}")
            if st["errors"] >= 5:
                break
            continue
        db.execute("""UPDATE recap_items SET state='published',remote_id=?,remote_url=?,published_hash=?,notes=?,updated_at=?
                      WHERE work_id=? AND episode_no=?""", (rid, url, h + "|" + args.status, f"{args.target}/{args.status}", P.now_iso(), wid, ep))
        db.commit()
        st["published"] += 1
        print(f"✓ {facts['title']} ح{ep} → {args.target} ({args.status})")
        if args.sleep:
            time.sleep(args.sleep)
    print("ملخص:", ", ".join(f"{k}={v}" for k, v in st.items()))
    return st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ملخصات الحلقات: elcinema + بحث ويب موثّق")
    ap.add_argument("--data-dir", default="elcinema_data")
    ap.add_argument("--min-year", type=int, default=2026)
    ap.add_argument("--max-year", type=int, default=0)
    ap.add_argument("--limit", type=int, default=10, help="أقصى عدد حلقات تُبحث/تُنشر في الدورة (تحكم بالتكلفة)")
    ap.add_argument("--min-sources", type=int, default=2, help="أقل عدد مواقع مستقلة لاعتماد حدث")
    ap.add_argument("--min-events", type=int, default=3)
    ap.add_argument("--target", choices=["jsonl", "wordpress", "webhook"], default="jsonl")
    ap.add_argument("--status", choices=["draft", "publish"], default="draft")
    ap.add_argument("--wp-type", default="posts")
    ap.add_argument("--out", default="elcinema_data/recaps.jsonl")
    ap.add_argument("--min-words", type=int, default=110)
    ap.add_argument("--credit-line", default="", help="سطر إفصاح اختياري يظهر أسفل المقال")
    ap.add_argument("--retry-held", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    st = run(a)
    return 0 if st["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
