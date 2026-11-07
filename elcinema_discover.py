#!/usr/bin/env python3
"""
elcinema_discover.py — الاستعانة بالذكاء الاصطناعي (Gemini أو DeepSeek) بحثًا في الويب لأمرين:
  1) discover: اكتشاف مسلسلات مصرية جديدة/معلنة (مثل رمضان 2027) لم تصل بعد إلى elcinema، وإضافتها
     إلى جدول works بمعرّف سالب (لا يتعارض مع معرّفات elcinema ولا مع تتبّع المعرّفات الجديدة).
  2) enrich: ملء ما نقص من أعمال elcinema (ملخص/أبطال/مخرج/جهة العرض) من نتائج بحث حقيقية.

قواعد الصدق: لا يُقبل عمل ولا معلومة إلا إن ذُكر لها مصدر (نطاق) ظهر فعلًا في نتائج البحث وليس elcinema.
يعمل مع LLM_PROVIDER=gemini (بحث جوجل المدمج) أو deepseek (واجهة البحث). طلب اكتشاف واحد كل ~20 ساعة،
وعدد صغير من طلبات الإكمال في الدورة، لتبقى الحصة المجانية كافية.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

import elcinema_publish as P
import elcinema_recaps as R

DDL = """
CREATE TABLE IF NOT EXISTS gemini_facts (
  work_id INTEGER PRIMARY KEY, data TEXT, domains TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS discover_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, found INTEGER, added INTEGER, note TEXT);
"""

DISCOVER_SYS = """أنت باحث دراما عربي. ابحث في الويب العربي عن المسلسلات المصرية الجديدة أو المعلن عنها.
أعد JSON فقط: {"works":[{"title":"اسم المسلسل بالعربية","year":2027,"status":"معلن|قيد التصوير|يُعرض حاليًا|انتهى عرضه",
"air_window":"موعد العرض المعلن كما ورد (مثل: رمضان 2027) أو فارغ","episodes_count":30,"cast":["اسم"],"director":"اسم أو فارغ",
"writer":"اسم أو فارغ","genre":"النوع","platform":"القناة أو المنصة المعلنة أو فارغ",
"synopsis":"فكرة العمل بصياغتك أنت في 25-60 كلمة مما أُعلن فقط","sources":["نطاق الموقع الذي وجدت فيه العمل"]}]}
- مصر فقط، مسلسلات فقط (لا أفلام ولا برامج). لا تخترع اسمًا ولا معلومة: ما لم تجده اتركه فارغًا.
- لا تنقل جملًا حرفية. أدرج في sources نطاقات المواقع التي ظهر فيها العمل فعلًا."""

ENRICH_SYS = """أنت باحث دراما عربي. ابحث في الويب عن معلومات مسلسل محدد، وأعد JSON فقط:
{"synopsis":"فكرة العمل بصياغتك في 25-60 كلمة أو فارغ","cast":["أبطال"],"director":"","writer":"","platform":"القناة أو المنصة الرسمية للعرض أو فارغ",
"sources":["نطاقات المواقع التي وجدت فيها المعلومات فعلًا"]}
- لا تخترع شيئًا: ما لم تجده اتركه فارغًا. لا تنقل جملًا حرفية."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def open_db(data_dir: str):
    db = P.open_db(data_dir)
    db.executescript(DDL)
    return db


# ───────────────────────── البحث (Gemini أو DeepSeek) ─────────────────────────
def search_json(system: str, user: str, queries: list[str], session=requests) -> tuple[dict, set]:
    """يعيد (كائن JSON، نطاقات نتائج البحث الحقيقية)."""
    prov, key, model = P.llm_config()
    if not key:
        raise SystemExit(f"مفتاح {prov} غير مضبوط")
    if prov == "gemini":
        j = P.gemini_post({"systemInstruction": {"parts": [{"text": system}]},
                           "contents": [{"role": "user", "parts": [{"text": user}]}],
                           "tools": [{"google_search": {}}],
                           "generationConfig": {"maxOutputTokens": 8192}}, model, key, session)
        c = (j.get("candidates") or [{}])[0]
        gm = c.get("groundingMetadata") or {}
        doms = {R.chunk_label(ch.get("web") or {}) for ch in gm.get("groundingChunks") or [] if ch.get("web")}
        doms.discard("")
        return R._parse_obj(P.gemini_text(j)), doms
    if prov == "deepseek":
        results, seen = [], set()
        for q in queries:
            for r in R.deepseek_search(q, key, session, count=10):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    results.append(r)
        results = results[:20]
        doms = {R.domain(r["url"]) for r in results}
        doms.discard("")
        lines = [f"[{n}] {R.domain(r['url'])} — {str(r.get('title') or '')[:120]}\n{snip(r)}" for n, r in enumerate(results, 1)]
        prompt = user + "\nنتائج البحث (استخدمها وحدها؛ في sources نطاقات هذه النتائج فقط):\n" + "\n\n".join(lines) + "\n\nأعد JSON فقط."
        return R._parse_obj(P.call_deepseek(system, prompt, model, key, session)), doms
    raise SystemExit("الاكتشاف يدعم LLM_PROVIDER=gemini أو deepseek فقط")


def snip(r: dict) -> str:
    return re.sub(r"\s+", " ", str(r.get("summary") or r.get("snippet") or "")).strip()[:700]


def good_sources(sources, doms: set) -> list[str]:
    out = []
    for s in sources or []:
        d = R.domain(str(s))
        if d in doms and "elcinema" not in d and d not in out:
            out.append(d)
    return out


def clean_list(v, n: int = 12) -> list[str]:
    return [re.sub(r"\s+", " ", str(x)).strip() for x in (v if isinstance(v, list) else []) if str(x).strip()][:n]


def clean_text(v, n: int = 400) -> str:
    t = re.sub(r"\s+", " ", str(v or "")).strip()
    return "" if re.search(r"https?:|www\.|[<>{}]", t) else t[:n]


# ───────────────────────── الاكتشاف ─────────────────────────
def synthetic_id(title: str, year: int) -> int:
    """معرّف سالب ثابت للعمل (لا يلتقي مع معرّفات elcinema الموجبة)."""
    return -(int(hashlib.sha1(f"{P.norm(title)}|{year}".encode()).hexdigest()[:8], 16) % 900_000_000 + 1)


def known_titles(db) -> set:
    return {(P.norm(r["title_ar"]).strip(), r["year"]) for r in db.execute("SELECT title_ar, year FROM works WHERE work_id>0 AND title_ar IS NOT NULL")}


def is_known(known: set, title: str, year: int) -> bool:
    t = P.norm(title).strip()
    return any(t == kt and abs((ky or year) - year) <= 1 for kt, ky in known)


def last_discovery_hours(db) -> float | None:
    r = db.execute("SELECT at FROM discover_log ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    return (now() - datetime.fromisoformat(r["at"])).total_seconds() / 3600


def discover(args, session=requests, search=search_json) -> dict:
    db = open_db(args.data_dir)
    st = dict(found=0, added=0, rejected=0, skipped_recent=0)
    hrs = last_discovery_hours(db)
    if hrs is not None and hrs < args.min_hours and not args.force:
        st["skipped_recent"] = 1
        return st
    y = now().year
    focus = args.focus or f"مسلسلات رمضان {y + 1} المعلنة، ومسلسلات {y} و{y + 1} الجديدة أو قيد التصوير"
    user = f"المطلوب: {focus}.\nأعد حتى {args.max_works} عملًا."
    queries = [f"مسلسلات رمضان {y + 1} المصرية قائمة الأعمال", f"مسلسلات مصرية جديدة {y + 1} قيد التصوير",
               f"مسلسلات مصرية جديدة {y} تُعرض الآن"]
    try:
        obj, doms = search(DISCOVER_SYS, user, queries, session)
    except RuntimeError as e:
        print(f"توقف الاكتشاف في هذه الدورة: {e}")
        st["api_stopped"] = 1
        return st
    items = obj.get("works") if isinstance(obj.get("works"), list) else []
    known = known_titles(db)
    for it in items[: args.max_works]:
        if not isinstance(it, dict):
            continue
        st["found"] += 1
        title = clean_text(it.get("title"), 80)
        try:
            year = int(it.get("year"))
        except (TypeError, ValueError):
            year = 0
        srcs = good_sources(it.get("sources"), doms)
        if not title or year < args.min_year or len(srcs) < args.min_sources or not re.search(r"[؀-ۿ]", title):
            st["rejected"] += 1
            continue
        if is_known(known, title, year):
            continue                                  # موجود أصلًا في elcinema
        wid = synthetic_id(title, year)
        extra = {"source": "ai-discovery", "domains": srcs, "air_window": clean_text(it.get("air_window"), 80),
                 "platform": clean_text(it.get("platform"), 40), "status": clean_text(it.get("status"), 30)}
        if db.execute("SELECT 1 FROM works WHERE work_id=?", (wid,)).fetchone():
            continue
        ts = now().isoformat(timespec="seconds")
        ec = it.get("episodes_count")
        db.execute(
            """INSERT INTO works (work_id, url, title_ar, year, category_id, category_label, is_series, status, synopsis,
               countries, genres, directors, writers, cast_top, episodes_count, release_dates, extra, first_seen_at, fetched_at, content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (wid, None, title, year, 3, "مسلسلات", 1, extra["status"], clean_text(it.get("synopsis"), 500),
             json.dumps(["مصر"], ensure_ascii=False),
             json.dumps(clean_list([it.get("genre")] if it.get("genre") else []), ensure_ascii=False),
             json.dumps(clean_list([it.get("director")] if it.get("director") else []), ensure_ascii=False),
             json.dumps(clean_list([it.get("writer")] if it.get("writer") else []), ensure_ascii=False),
             json.dumps(clean_list(it.get("cast")), ensure_ascii=False),
             ec if isinstance(ec, int) and 0 < ec < 200 else None,
             json.dumps([extra["air_window"]] if extra["air_window"] else [], ensure_ascii=False),
             json.dumps(extra, ensure_ascii=False), ts, ts,
             hashlib.sha1(json.dumps(it, sort_keys=True, ensure_ascii=False).encode()).hexdigest()))
        st["added"] += 1
        known.add((P.norm(title).strip(), year))
    db.execute("INSERT INTO discover_log (at, found, added, note) VALUES (?,?,?,?)",
               (now().isoformat(timespec="seconds"), st["found"], st["added"], f"rejected={st['rejected']}"))
    db.commit()
    return st


# ───────────────────────── الإكمال عند نقص المعلومات ─────────────────────────
def enrich(args, session=requests, search=search_json) -> dict:
    db = open_db(args.data_dir)
    st = dict(enriched=0, empty=0)
    rows = db.execute(
        """SELECT w.work_id, w.title_ar, w.year, w.synopsis, w.cast_top FROM works w
           LEFT JOIN gemini_facts g ON g.work_id=w.work_id
           WHERE w.work_id>0 AND w.is_series=1 AND w.year>=? AND g.work_id IS NULL
             AND (COALESCE(w.synopsis,'')='' OR COALESCE(w.cast_top,'') IN ('','[]'))
             AND COALESCE(w.countries,'') LIKE '%مصر%'
           ORDER BY w.first_seen_at DESC LIMIT ?""", (args.min_year, args.enrich_limit)).fetchall()
    for r in rows:
        user = f"المسلسل: {r['title_ar']} ({r['year']})"
        try:
            obj, doms = search(ENRICH_SYS, user, [f"مسلسل {r['title_ar']} {r['year']} قصة أبطال", f"مسلسل {r['title_ar']} أين يعرض"], session)
        except RuntimeError as e:
            print(f"توقف الإكمال في هذه الدورة: {e}")
            st["api_stopped"] = 1
            break
        srcs = good_sources(obj.get("sources"), doms)
        data = {}
        if len(srcs) >= args.min_sources:
            data = {"synopsis": clean_text(obj.get("synopsis"), 500), "cast": clean_list(obj.get("cast")),
                    "director": clean_text(obj.get("director"), 60), "writer": clean_text(obj.get("writer"), 60),
                    "platform": clean_text(obj.get("platform"), 40)}
            data = {k: v for k, v in data.items() if v}
        db.execute("INSERT OR REPLACE INTO gemini_facts (work_id, data, domains, fetched_at) VALUES (?,?,?,?)",
                   (r["work_id"], json.dumps(data, ensure_ascii=False), json.dumps(srcs, ensure_ascii=False),
                    now().isoformat(timespec="seconds")))
        st["enriched" if data else "empty"] += 1
    db.commit()
    return st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="اكتشاف أعمال جديدة وإكمال النقص عبر بحث الذكاء الاصطناعي")
    ap.add_argument("--data-dir", default="elcinema_data")
    ap.add_argument("--min-year", type=int, default=2026)
    ap.add_argument("--min-sources", type=int, default=1)
    ap.add_argument("--max-works", type=int, default=15)
    ap.add_argument("--min-hours", type=float, default=20, help="أقل فاصل بين عمليتي اكتشاف")
    ap.add_argument("--enrich-limit", type=int, default=3)
    ap.add_argument("--focus", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-discover", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    a = ap.parse_args(argv)
    out = {}
    if not a.no_discover:
        out["discover"] = discover(a)
    if not a.no_enrich:
        out["enrich"] = enrich(a)
    print("ملخص:", json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
