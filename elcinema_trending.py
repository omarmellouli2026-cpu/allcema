#!/usr/bin/env python3
"""
elcinema_trending.py — ترتيب يومي تقديري للمسلسلات والأفلام المصرية الأكثر بحثًا ومشاهدة.

1) يسأل Gemini (مع بحث جوجل المدمج) عن الأعمال المصرية الأكثر بحثًا وتداولًا هذه الأيام.
2) لا يُقبل عمل إلا إن ذُكر له مصدر ظهر فعلًا في نتائج البحث (قاعدة الصدق نفسها في elcinema_discover).
3) يطابق كل عنوان مع جدول works (elcinema) بالاسم والسنة وبلد الإنتاج مصر.
4) يكتب elcinema_data/trending.json (يقرأه الموقع)، ويكتب قوائم الأعمال الجديدة المؤهلة للنشر
   (مصر، السنة >= --min-year، لم تُنشر بعد) في ملفين نصيين ليكمل بها الـworkflow: جلب ← كتابة ← نشر.

الترتيب تقديري من نتائج الويب وليس إحصاء رسميًا للمشاهدات، ويُكتب ذلك في الموقع صراحة.
فشل Gemini لا يكسر الدورة: يبقى آخر trending.json كما هو.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import elcinema_discover as D
import elcinema_publish as P

SYS = """أنت محلل محتوى عربي. ابحث في الويب عن المسلسلات والأفلام المصرية الأكثر بحثًا ومشاهدة وتداولًا هذه الأيام
(اعتمد على نتائج حديثة: منصات المشاهدة والأخبار والترندات). أعد JSON فقط بهذا الشكل:
{"series":[{"rank":1,"title":"اسم المسلسل بالعربية","year":2026,"why":"جملة قصيرة بصياغتك عن سبب انتشاره","sources":["نطاقات المواقع"]}],
 "films":[{"rank":1,"title":"اسم الفيلم بالعربية","year":2026,"why":"","sources":[]}]}
- أعمال مصرية فقط. رتّبها من الأكثر إلى الأقل. لا تخترع اسمًا ولا معلومة: ما لم تجده لا تذكره.
- لا تنقل جملًا حرفية من المواقع. في sources نطاقات المواقع التي ظهر فيها العمل فعلًا في نتائج البحث."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def clean_title(t: str) -> str:
    return D.clean_text(t, 80)


def variants(t: str) -> list[str]:
    """العنوان كما ورد ثم بدون كلمة «مسلسل/فيلم» في أوله (قد تكون جزءًا من الاسم الحقيقي)."""
    s = re.sub(r"^(مسلسل|فيلم)\s+", "", t).strip()
    return [t] + ([s] if s and s != t else [])


def parse_items(obj, doms: set, limit: int) -> list[dict]:
    out, seen = [], set()
    for it in (obj if isinstance(obj, list) else []):
        if not isinstance(it, dict):
            continue
        title = clean_title(it.get("title"))
        key = P.norm(title).strip()
        srcs = D.good_sources(it.get("sources"), doms)
        if not title or key in seen or not re.search(r"[\u0600-\u06ff]", title) or not srcs:
            continue
        try:
            year = int(it.get("year"))
        except (TypeError, ValueError):
            year = 0
        seen.add(key)
        out.append({"title": title, "year": year, "why": D.clean_text(it.get("why"), 140), "sources": srcs[:4]})
        if len(out) >= limit:
            break
    return out


def index_works(db, is_series: int) -> dict:
    idx: dict[str, list] = {}
    for r in db.execute("SELECT work_id, title_ar, year, countries FROM works WHERE work_id>0 AND is_series=? AND title_ar IS NOT NULL", (is_series,)):
        if "مصر" not in (r["countries"] or ""):
            continue
        idx.setdefault(P.norm(r["title_ar"]).strip(), []).append(r)
    return idx


def match(idx: dict, title: str, year: int):
    for v in variants(title):
        c = [r for r in idx.get(P.norm(v).strip(), []) if not year or not r["year"] or abs(r["year"] - year) <= 1]
        if c:
            c.sort(key=lambda r: -(r["year"] or 0))
            return c[0]
    return None


def published_ids(db) -> set:
    try:
        return {r[0] for r in db.execute("SELECT work_id FROM site_items")}
    except Exception:
        return set()


def run(args, search=D.search_json) -> dict:
    db = P.open_db(args.data_dir)
    y = now().year
    user = (f"التاريخ: {now().date().isoformat()}. المطلوب: المسلسلات المصرية والأفلام المصرية الأكثر بحثًا ومشاهدة الآن "
            f"(أعمال {y - 1} و{y} و{y + 1} وما يُعرض حاليًا). أعد حتى {args.top} مسلسلًا وحتى {args.top} فيلمًا.")
    queries = ["أكثر المسلسلات المصرية مشاهدة هذا الأسبوع", "أكثر الأفلام المصرية مشاهدة هذا الأسبوع", "ترند مسلسلات مصرية الآن"]
    st = {"series": 0, "films": 0, "matched": 0, "new_series": [], "new_films": [], "unmatched": []}
    obj = None
    try:
        obj, doms = search(SYS, user, queries, requests)
    except (SystemExit, Exception) as e:  # Gemini أولًا؛ عند فشله (مثل حد الحصة 429) نجرّب DeepSeek إن توفّر مفتاحه
        print(f"تعذّر البحث بالمزوّد الأول: {str(e)[:160]}")
        st["stopped"] = str(e)[:200]
        if os.environ.get("DEEPSEEK_API_KEY") and (os.environ.get("LLM_PROVIDER") or "").strip().lower() != "deepseek":
            os.environ["LLM_PROVIDER"] = "deepseek"
            try:
                obj, doms = search(SYS, user, queries, requests)
                st.pop("stopped", None)
                st["provider"] = "deepseek"
            except (SystemExit, Exception) as e2:
                print(f"تعذّر DeepSeek أيضًا: {str(e2)[:160]}")
                st["stopped"] = (st["stopped"] + " | " + str(e2))[:300]
        if obj is None:
            return st                                   # لا نكسر الدورة؛ يبقى الترتيب السابق
    done = published_ids(db)
    result = {"date": now().date().isoformat(), "generated_at": now().isoformat(timespec="seconds"),
              "note": "ترتيب تقديري من نتائج البحث على الويب وليس إحصاء رسميًا للمشاهدات.", "series": [], "films": []}
    for kind, is_series, key in (("series", 1, "series"), ("films", 0, "films")):
        items = parse_items(obj.get(key) if isinstance(obj, dict) else None, doms, args.top)
        idx = index_works(db, is_series)
        for i, it in enumerate(items, 1):
            w = match(idx, it["title"], it["year"])
            e = {"rank": i, "title": (w["title_ar"] if w else variants(it["title"])[-1]), "year": it["year"], "why": it["why"], "sources": it["sources"], "work_id": int(w["work_id"]) if w else None}
            result[kind].append(e)
            st[kind] += 1
            if not w:
                if it["year"] >= args.min_year:
                    st["unmatched"].append(it["title"])
                continue
            st["matched"] += 1
            if (w["year"] or 0) >= args.min_year and int(w["work_id"]) not in done and len(st["new_" + kind]) < args.max_new:
                st["new_" + kind].append(int(w["work_id"]))
    if not (result["series"] or result["films"]):
        print("لم يُقبل أي عمل (لا مصادر في نتائج البحث)؛ يبقى الترتيب السابق.")
        return st
    out = Path(args.data_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "trending.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ids = Path(args.ids_dir)
    ids.mkdir(parents=True, exist_ok=True)
    (ids / "new_series.txt").write_text(" ".join(map(str, st["new_series"])), encoding="utf-8")
    (ids / "new_films.txt").write_text(" ".join(map(str, st["new_films"])), encoding="utf-8")
    return st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ترتيب يومي للأعمال المصرية الأكثر بحثًا")
    ap.add_argument("--data-dir", default="elcinema_data")
    ap.add_argument("--ids-dir", default="/tmp/trending_ids", help="مجلد ملفات المعرّفات الجديدة (مؤقت، لا يُحفظ في المستودع)")
    ap.add_argument("--min-year", type=int, default=2026, help="لا نكتب إلا عن الأعمال من هذه السنة فصاعدًا (ضبط التكلفة)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-new", type=int, default=4, help="حد الأعمال الجديدة لكل نوع في اليوم")
    a = ap.parse_args(argv)
    Path(a.ids_dir).mkdir(parents=True, exist_ok=True)
    for n in ("new_series.txt", "new_films.txt"):
        (Path(a.ids_dir) / n).write_text("", encoding="utf-8")
    st = run(a)
    print(json.dumps(st, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
