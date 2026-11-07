#!/usr/bin/env python3
"""
elcinema_publish.py — الحلقة الثانية من الخط الآلي:
    الأعمال المجلوبة (elcinema.sqlite)  →  تحويل إلى محتوى أصلي مختلف  →  بوابة جودة/عدم‑نسخ  →  نشر

لا يغيّر شيئًا في قاعدة الجلب إلا جدولًا جديدًا اسمه site_items. آمن للتشغيل المتكرر (idempotent):
كل عمل يُحوَّل وينشر مرة واحدة، ثم يُحدَّث فقط إذا تغيّرت بياناته.

المتغيرات البيئية:
  ANTHROPIC_API_KEY  (مطلوب للتحويل)    ANTHROPIC_MODEL (اختياري)
  WP_URL / WP_USER / WP_APP_PASSWORD     (هدف wordpress)
  WEBHOOK_URL / WEBHOOK_TOKEN            (هدف webhook)
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

DEFAULT_MODEL = "claude-haiku-5-5"
FORBIDDEN = ("elcinema", "السينما.كوم", "اقرأ المزيد", "ملخص القصة", "تعليقات المستخدمين")
MAX_SHINGLE_OVERLAP = 0.08     # أقصى نسبة عبارات (4 كلمات) مشتركة مع النص المصدر
MAX_COPY_RUN = 6               # أطول مقطع متتابع (بالكلمات) يُسمح أن يتطابق مع المصدر

DDL = """
CREATE TABLE IF NOT EXISTS site_items (
  work_id INTEGER PRIMARY KEY, state TEXT, facts_hash TEXT, article TEXT,
  remote_id TEXT, remote_url TEXT, attempts INTEGER DEFAULT 0, notes TEXT,
  updated_at TEXT, published_hash TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ───────────────────────── أدوات نصية ─────────────────────────
_DIAC = re.compile(r"[ً-ْـ]")


def norm(s: str) -> str:
    s = _DIAC.sub("", s or "")
    s = re.sub("[إأآ]", "ا", s).replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"[^\w\s]", " ", s.lower())


def words(s: str) -> list[str]:
    return norm(s).split()


def shingles(w: list[str], n: int = 4) -> set:
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def overlap_ratio(article: str, source: str) -> float:
    a, s = shingles(words(article)), shingles(words(source))
    return len(a & s) / max(1, len(a))


def longest_copy_run(article: str, source: str) -> int:
    wa, ws = words(article), words(source)
    if not wa or not ws:
        return 0
    return SequenceMatcher(None, wa, ws, autojunk=False).find_longest_match(0, len(wa), 0, len(ws)).size


def as_list(v) -> list:
    if v in (None, ""):
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return [x.strip() for x in re.split(r"[،,|]", v) if x.strip()]
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                x = x.get("name") or x.get("title") or x.get("label") or ""
            if str(x).strip():
                out.append(str(x).strip())
        return out
    return [str(v)]


# ───────────────────────── قاعدة البيانات والحقائق ─────────────────────────
def open_db(data_dir: str) -> sqlite3.Connection:
    p = Path(data_dir) / "elcinema.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(DDL)
    return c


def scope_sql(args, alias: str = "") -> tuple[str, list]:
    """شرط SQL يحصر ما يُرسَل إلى LLM (للتحكم بالتكلفة): مسلسلات + بلد إنتاج محدد.
    الاستدعاء البرمجي بلا هذه الخيارات لا يُفلتَر (توافق مع الاختبارات)."""
    p = f"{alias}." if alias else ""
    sql, params = "", []
    if getattr(args, "only_series", False):
        sql += f" AND {p}is_series=1"
    for cn in [x.strip() for x in (getattr(args, "countries", "") or "").split(",") if x.strip()]:
        sql += f" AND {p}countries LIKE ?"
        params.append(f"%{cn}%")
    return sql, params


def build_facts(c: sqlite3.Connection, wid: int, min_year: int) -> tuple[dict, str]:
    """يعيد (حقائق وقائعية قابلة للنشر، نص المصدر الخاص الذي يُقارَن به ولا يُنشر)."""
    w = dict(c.execute("SELECT * FROM works WHERE work_id=?", (wid,)).fetchone())
    credits = c.execute("SELECT person_name, section FROM credits WHERE work_id=? ORDER BY rowid", (wid,)).fetchall()
    cast = [r["person_name"] for r in credits if r["section"] and ("cast" in r["section"].lower() or "تمثيل" in r["section"])]
    cast = cast or as_list(w.get("cast_top"))
    try:
        related = c.execute(
            """SELECT w2.work_id, w2.title_ar, si.remote_url, COUNT(*) n
               FROM credits a JOIN credits b ON a.person_id=b.person_id AND b.work_id<>a.work_id
               JOIN works w2 ON w2.work_id=b.work_id
               LEFT JOIN site_items si ON si.work_id=w2.work_id
               WHERE a.work_id=? AND w2.year>=? GROUP BY w2.work_id HAVING n>=2 ORDER BY n DESC LIMIT 5""",
            (wid, min_year)).fetchall()
        airings = c.execute("SELECT channel_name, date_text, time_raw FROM tv_airings WHERE work_id=? LIMIT 6", (wid,)).fetchall()
    except sqlite3.Error:
        related, airings = [], []
    facts = {
        "work_id": wid,
        "title": w.get("title_ar"), "alt_title": w.get("title_alt"), "year": w.get("year"),
        "kind": "مسلسل" if w.get("is_series") else "فيلم",
        "category": w.get("category_label"), "genres": as_list(w.get("genres")),
        "countries": as_list(w.get("countries")), "languages": as_list(w.get("languages")),
        "duration_min": w.get("duration_min"), "episodes_count": w.get("episodes_count"),
        "directors": as_list(w.get("directors")), "writers": as_list(w.get("writers")),
        "cast": cast[:10], "release_dates": as_list(w.get("release_dates")),
        "tv_airings": [{"channel": r["channel_name"], "date": r["date_text"], "time": r["time_raw"]} for r in airings],
        "related": [{"title": r["title_ar"], "url": r["remote_url"]} for r in related],
    }
    eps = [r["summary"] for r in c.execute("SELECT summary FROM episodes WHERE work_id=? ORDER BY episode_no", (wid,)) if r["summary"]]
    source = " ".join([w.get("synopsis") or ""] + eps)
    return facts, source


def facts_hash(facts: dict, source: str) -> str:
    f = {k: v for k, v in facts.items() if k != "related"}      # الروابط الداخلية لا تستدعي إعادة توليد
    return hashlib.sha1(json.dumps([f, source], sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# ───────────────────────── التحويل (LLM) ─────────────────────────
SYSTEM = """أنت محرر عربي محترف لموقع سينما ودراما. تكتب مقالات أصلية بأسلوبك وبنيتك أنت.
قواعد صارمة:
- لا تنسخ أي عبارة من «الملخص المصدر»؛ استخرج الأفكار وأعد بناءها بصياغة وترتيب مختلفين تمامًا.
- لا تخترع أي معلومة (أسماء، أحداث، تواريخ، جوائز) غير موجودة في الحقائق أو الملخص. إن غابت معلومة فلا تذكرها.
- لا تذكر أسماء مواقع أخرى ولا آراء مستخدمين ولا تقييمات.
- لغة عربية سليمة وطبيعية، دون حشو أو تكرار، وبنبرة تحريرية مفيدة للقارئ.
أعد JSON فقط بالمفاتيح: headline (عنوان جذاب ≤ 70 حرفًا)، intro (فقرة افتتاحية 40-70 كلمة)،
overview (3 فقرات عن القصة والأجواء 150-260 كلمة، بفقرات مفصولة بسطر فارغ)،
highlights (قائمة 3-5 نقاط قصيرة: لماذا يستحق الاهتمام)، faq (قائمة 3 عناصر {q,a} تُجاب من الحقائق فقط)."""


GEMINI_MODEL = "gemini-3.5-flash"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_URL = "https://api.deepseek.com"


def llm_config() -> tuple[str, str | None, str]:
    """(المزوّد، المفتاح، النموذج). LLM_PROVIDER=deepseek|gemini|anthropic؛ وإن لم يُحدَّد: gemini
    عند وجود GEMINI_API_KEY وغياب ANTHROPIC_API_KEY، وإلا anthropic."""
    prov = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if not prov:
        prov = "gemini" if os.environ.get("GEMINI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY") else "anthropic"
    if prov == "deepseek":
        return prov, os.environ.get("DEEPSEEK_API_KEY"), os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_MODEL
    if prov == "gemini":
        return prov, os.environ.get("GEMINI_API_KEY"), os.environ.get("GEMINI_MODEL") or GEMINI_MODEL
    return "anthropic", os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def gemini_post(body: dict, model: str, key: str, session=requests) -> dict:
    last = None
    for i in range(4):
        try:
            r = session.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                             headers={"x-goog-api-key": key, "content-type": "application/json"}, json=body, timeout=180)
            if r.status_code in (429, 500, 502, 503, 529):
                last = f"http {r.status_code}: {r.text[:300]}"
                if i < 3:
                    time.sleep((5, 20, 60)[i])
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 ** i * 3)
    raise RuntimeError(f"Gemini failed: {last}")


def gemini_text(j: dict) -> str:
    c = (j.get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in ((c.get("content") or {}).get("parts") or []) if not p.get("thought"))


def call_gemini(system: str, user: str, model: str, key: str, session=requests) -> str:
    j = gemini_post({"systemInstruction": {"parts": [{"text": system}]},
                     "contents": [{"role": "user", "parts": [{"text": user}]}],
                     "generationConfig": {"maxOutputTokens": 4096, "responseMimeType": "application/json"}},
                    model, key, session)
    return gemini_text(j)


def deepseek_post(path: str, body: dict, key: str, session=requests) -> dict:
    """طلب إلى DeepSeek (متوافق مع OpenAI). إعادة محاولة قصيرة؛ 429/5xx تُعاد ثم يُرفع خطأ يوقف الدورة بهدوء."""
    last = None
    for i in range(4):
        try:
            r = session.post(f"{DEEPSEEK_URL}{path}", json=body, timeout=180,
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"http {r.status_code}: {r.text[:300]}"
                if i < 3:
                    time.sleep((3, 10, 30)[i])
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 ** i * 3)
    raise RuntimeError(f"DeepSeek failed: {last}")


def deepseek_text(j: dict) -> str:
    return (((j.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""


def call_deepseek(system: str, user: str, model: str, key: str, session=requests) -> str:
    j = deepseek_post("/chat/completions", {
        "model": model, "max_tokens": 4096, "stream": False,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, key, session)
    return deepseek_text(j)


def call_llm(system: str, user: str, model: str, key: str, session=requests) -> str:
    if llm_config()[0] == "deepseek":
        return call_deepseek(system, user, model, key, session)
    if llm_config()[0] == "gemini":
        return call_gemini(system, user, model, key, session)
    last = None
    for i in range(4):
        try:
            r = session.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                             json={"model": model, "max_tokens": 3000, "system": system,
                                   "messages": [{"role": "user", "content": user}]}, timeout=120)
            if r.status_code in (429, 500, 502, 503, 529):
                last = f"http {r.status_code}"
                time.sleep(2 ** i * 3)
                continue
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2 ** i * 3)
    raise RuntimeError(f"LLM failed: {last}")


def parse_article(text: str) -> dict | None:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        d = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    d["headline"] = str(d.get("headline") or "").strip()
    d["intro"] = str(d.get("intro") or "").strip()
    d["overview"] = str(d.get("overview") or "").strip()
    d["highlights"] = [str(x).strip() for x in (d.get("highlights") or []) if str(x).strip()]
    d["faq"] = [{"q": str(x.get("q", "")).strip(), "a": str(x.get("a", "")).strip()}
                for x in (d.get("faq") or []) if isinstance(x, dict) and x.get("q") and x.get("a")]
    return d


def article_text(a: dict) -> str:
    return " ".join([a["headline"], a["intro"], a["overview"], *a["highlights"], *[f"{x['q']} {x['a']}" for x in a["faq"]]])


def template_article(f: dict) -> dict:
    """بديل بلا LLM: صفحة وقائعية فقط (بلا ملخص)، لا تُنشر إلا كمسودة."""
    bits = [f["kind"], f["title"]] + ([f"({f['year']})"] if f.get("year") else [])
    cast = "، ".join(f["cast"][:5])
    intro = " ".join(bits) + (f" من بطولة {cast}." if cast else ".")
    hl = []
    if f["genres"]:
        hl.append("النوع: " + "، ".join(f["genres"]))
    if f["directors"]:
        hl.append("إخراج: " + "، ".join(f["directors"]))
    if f["countries"]:
        hl.append("بلد الإنتاج: " + "، ".join(f["countries"]))
    return {"headline": f"{f['kind']} {f['title']}", "intro": intro, "overview": "", "highlights": hl, "faq": []}


def gate(a: dict, source: str, facts: dict, min_words: int) -> list[str]:
    """قائمة أسباب الرفض؛ فارغة = ناجح."""
    why = []
    if not (a["headline"] and a["intro"]):
        why.append("حقول ناقصة")
    txt = article_text(a)
    n = len(words(txt))
    if n < min_words:
        why.append(f"قصير ({n} كلمة)")
    low = txt.lower()
    for bad in FORBIDDEN:
        if bad.lower() in low:
            why.append(f"عبارة ممنوعة: {bad}")
    if source.strip():
        ov, run = overlap_ratio(txt, source), longest_copy_run(txt, source)
        if ov > MAX_SHINGLE_OVERLAP:
            why.append(f"تشابه عالٍ مع المصدر ({ov:.0%})")
        if run > MAX_COPY_RUN:
            why.append(f"مقطع منسوخ من {run} كلمات")
    if facts.get("title") and norm(facts["title"]) not in norm(txt):
        why.append("العنوان غير مذكور")
    return why


def generate(facts: dict, source: str, args, llm) -> tuple[dict | None, str]:
    if args.template_only:
        a = template_article(facts)
        why = gate(a, "", facts, 15)
        return (None, "؛ ".join(why)) if why else (a, "template-only")
    prov, key, model = llm_config()
    if not key:
        raise SystemExit(f"مفتاح {prov} غير مضبوط (ANTHROPIC_API_KEY أو GEMINI_API_KEY أو DEEPSEEK_API_KEY) أو استخدم --template-only")
    pub_facts = {k: v for k, v in facts.items() if k not in ("work_id", "related") and v not in (None, [], "")}
    user = ("الحقائق (JSON):\n" + json.dumps(pub_facts, ensure_ascii=False) +
            "\n\nالملخص المصدر (للفهم فقط — ممنوع النسخ منه):\n" + (source[:4000] or "(لا يوجد)"))
    notes = ""
    for attempt in range(2):
        prompt = user if attempt == 0 else (user + "\n\nملاحظة: المحاولة السابقة رُفضت (" + notes +
                                              "). أعد الكتابة بصياغة وبنية مختلفتين جذريًا.")
        a = parse_article(llm(SYSTEM, prompt, model, key))
        if a is None:
            notes = "مخرجات غير صالحة"
            continue
        why = gate(a, source, facts, args.min_words)
        if not why:
            return a, "ok"
        notes = "؛ ".join(why)
    return None, notes


# ───────────────────────── العرض والنشر ─────────────────────────
def e(s) -> str:
    return htmllib.escape(str(s))


def render_html(a: dict, f: dict, credit: str = "") -> str:
    out = ["<p><strong>تنبيه: يتضمن حرقًا لأحداث الحلقة.</strong></p>"] if f.get("spoiler") else []
    out.append(f"<p>{e(a['intro'])}</p>")
    rows = [("النوع", f["kind"]), ("سنة الإنتاج", f.get("year")), ("التصنيف", "، ".join(f["genres"])),
            ("الإخراج", "، ".join(f["directors"])), ("التأليف", "، ".join(f["writers"])),
            ("البطولة", "، ".join(f["cast"])), ("بلد الإنتاج", "، ".join(f["countries"])),
            ("المدة (دقيقة)", f.get("duration_min")), ("عدد الحلقات", f.get("episodes_count"))]
    rows = [(k, v) for k, v in rows if v not in (None, "", [])]
    if rows:
        out.append("<h2>بطاقة العمل</h2><table>" + "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows) + "</table>")
    if a["overview"]:
        out.append("<h2>نظرة على العمل</h2>" + "".join(f"<p>{e(p.strip())}</p>" for p in a["overview"].split("\n\n") if p.strip()))
    if a["highlights"]:
        out.append("<h2>لماذا يستحق المتابعة</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in a["highlights"]) + "</ul>")
    if f["tv_airings"]:
        out.append("<h2>مواعيد العرض التلفزيوني</h2><ul>" + "".join(
            f"<li>{e(x['channel'])} — {e(x['date'])} {e(x['time'])}</li>" for x in f["tv_airings"]) + "</ul>")
    if a["faq"]:
        out.append("<h2>أسئلة شائعة</h2>" + "".join(f"<h3>{e(x['q'])}</h3><p>{e(x['a'])}</p>" for x in a["faq"]))
    if f["related"]:
        li = "".join(f'<li><a href="{e(r["url"])}">{e(r["title"])}</a></li>' if r["url"] else f"<li>{e(r['title'])}</li>" for r in f["related"])
        out.append(f"<h2>أعمال ذات صلة</h2><ul>{li}</ul>")
    if credit:
        out.append(f"<p><small>{e(credit)}</small></p>")
    return "\n".join(out)


def schema_ld(f: dict, a: dict) -> dict:
    d = {"@context": "https://schema.org", "@type": f.get("schema_type") or ("TVSeries" if f["kind"] == "مسلسل" else "Movie"),
         "name": f["title"], "description": a["intro"]}
    if f.get("episode_no"):
        d["name"] = f"{f['title']} الحلقة {f['episode_no']}"
        d["episodeNumber"] = f["episode_no"]
        d["partOfSeries"] = {"@type": "TVSeries", "name": f["title"]}
    if f.get("year"):
        d["datePublished"] = str(f["year"])
    if f["genres"]:
        d["genre"] = f["genres"]
    if f["cast"]:
        d["actor"] = [{"@type": "Person", "name": n} for n in f["cast"]]
    if f["directors"]:
        d["director"] = [{"@type": "Person", "name": n} for n in f["directors"]]
    if f.get("episodes_count"):
        d["numberOfEpisodes"] = f["episodes_count"]
    return d


def publish(args, session, wid: int, a: dict, f: dict, prev_remote: str | None) -> tuple[str, str | None]:
    html = render_html(a, f, args.credit_line)
    if args.target == "jsonl":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"work_id": wid, "status": args.status, "title": a["headline"], "excerpt": a["intro"],
                                 "html": html, "schema": schema_ld(f, a), "facts": f, "at": now_iso()},
                                ensure_ascii=False) + "\n")
        return str(wid), None
    if args.target == "wordpress":
        base = os.environ["WP_URL"].rstrip("/")
        auth = (os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"])
        body = {"title": a["headline"], "content": html, "excerpt": a["intro"][:300], "status": args.status}

        def send(rid):
            url = f"{base}/wp-json/wp/v2/{args.wp_type}" + (f"/{rid}" if rid else "")
            return session.post(url, json=body, auth=auth, timeout=60)
        r = send(prev_remote)
        if r.status_code == 404 and prev_remote:       # حُذف من الموقع يدويًا: أنشئه من جديد
            r = send(None)
        r.raise_for_status()
        j = r.json()
        return str(j["id"]), j.get("link")
    if args.target == "webhook":
        hdr = {"Authorization": f"Bearer {os.environ['WEBHOOK_TOKEN']}"} if os.environ.get("WEBHOOK_TOKEN") else {}
        r = session.post(os.environ["WEBHOOK_URL"], headers=hdr, timeout=60, json={
            "work_id": wid, "remote_id": prev_remote, "status": args.status, "title": a["headline"],
            "excerpt": a["intro"], "html": html, "schema": schema_ld(f, a), "facts": f})
        r.raise_for_status()
        try:
            j = r.json()
        except ValueError:
            j = {}
        return str(j.get("id") or prev_remote or wid), j.get("url")
    raise SystemExit(f"هدف غير معروف: {args.target}")


# ───────────────────────── التشغيل ─────────────────────────
def run(args, llm=call_llm, session=None) -> dict:
    session = session or requests.Session()
    db = open_db(args.data_dir)
    if args.template_only:
        args.status = "draft"                      # صفحة وقائعية رقيقة: لا تُنشر علنًا أبدًا
    st = dict(generated=0, published=0, held=0, errors=0, skipped=0)
    scope, sp = scope_sql(args)
    ids = [r["work_id"] for r in db.execute(
        "SELECT work_id FROM works WHERE year>=? " + ("AND year<=? " if args.max_year else "") + scope +
        " ORDER BY first_seen_at DESC, work_id DESC",
        ((args.min_year, args.max_year) if args.max_year else (args.min_year,)) + tuple(sp))]
    for wid in ids:
        if st["generated"] >= args.limit and st["published"] >= args.limit:
            break
        facts, source = build_facts(db, wid, args.min_year)
        h = facts_hash(facts, source)
        row = db.execute("SELECT * FROM site_items WHERE work_id=?", (wid,)).fetchone()
        if row and row["state"] == "published" and row["published_hash"] == h + "|" + args.status:
            st["skipped"] += 1
            continue
        if row and row["state"] == "held" and row["facts_hash"] == h and not args.retry_held:
            st["skipped"] += 1
            continue
        article = json.loads(row["article"]) if row and row["article"] and row["facts_hash"] == h and row["state"] in ("ready", "error", "published") else None
        if article is None:
            if st["generated"] >= args.limit:
                continue
            article, notes = generate(facts, source, args, llm)
            st["generated"] += 1
            if article is None:
                db.execute("""INSERT INTO site_items(work_id,state,facts_hash,notes,updated_at) VALUES(?,?,?,?,?)
                              ON CONFLICT(work_id) DO UPDATE SET state='held',facts_hash=excluded.facts_hash,
                              notes=excluded.notes,updated_at=excluded.updated_at""", (wid, "held", h, notes, now_iso()))
                db.commit()
                st["held"] += 1
                print(f"✗ {wid} محجوز: {notes}")
                continue
            db.execute("""INSERT INTO site_items(work_id,state,facts_hash,article,notes,updated_at) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(work_id) DO UPDATE SET state='ready',facts_hash=excluded.facts_hash,
                          article=excluded.article,notes=excluded.notes,updated_at=excluded.updated_at""",
                       (wid, "ready", h, json.dumps(article, ensure_ascii=False), notes, now_iso()))
            db.commit()
        if args.no_publish:
            continue
        if st["published"] >= args.limit:
            continue
        try:
            rid, url = publish(args, session, wid, article, facts, row["remote_id"] if row else None)
        except Exception as ex:                    # noqa: BLE001 — لا نوقف الدورة بسبب عمل واحد
            db.execute("UPDATE site_items SET state='error',attempts=attempts+1,notes=?,updated_at=? WHERE work_id=?",
                       (str(ex)[:300], now_iso(), wid))
            db.commit()
            st["errors"] += 1
            print(f"! {wid} فشل النشر: {ex}")
            if st["errors"] >= 5:
                print("أخطاء متتالية كثيرة؛ أوقف الدورة")
                break
            continue
        db.execute("""UPDATE site_items SET state='published',remote_id=?,remote_url=?,published_hash=?,notes=?,updated_at=?
                      WHERE work_id=?""", (rid, url, h + "|" + args.status, f"{args.target}/{args.status}", now_iso(), wid))
        db.commit()
        st["published"] += 1
        print(f"✓ {wid} {facts['title']} → {args.target} ({args.status})")
        if args.sleep:
            time.sleep(args.sleep)
    print("ملخص:", ", ".join(f"{k}={v}" for k, v in st.items()))
    return st


def cmd_status(args) -> None:
    db = open_db(args.data_dir)
    for r in db.execute("SELECT state, COUNT(*) n FROM site_items GROUP BY state"):
        print(f"{r['state']:<10}{r['n']}")
    held = db.execute("SELECT work_id, notes FROM site_items WHERE state='held' ORDER BY updated_at DESC LIMIT 10").fetchall()
    for r in held:
        print(f"  محجوز {r['work_id']}: {r['notes']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="تحويل الأعمال المجلوبة إلى محتوى أصلي ونشرها")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data-dir", default="elcinema_data")
    r.add_argument("--min-year", type=int, default=2026)
    r.add_argument("--max-year", type=int, default=0)
    r.add_argument("--limit", type=int, default=20, help="أقصى عدد توليد/نشر في الدورة الواحدة (تحكم بالتكلفة)")
    r.add_argument("--target", choices=["jsonl", "wordpress", "webhook"], default="jsonl")
    r.add_argument("--status", choices=["draft", "publish"], default="draft")
    r.add_argument("--wp-type", default="posts", help="نوع المحتوى في ووردبريس (posts أو نوع مخصّص)")
    r.add_argument("--out", default="elcinema_data/site_items.jsonl")
    r.add_argument("--min-words", type=int, default=180)
    r.add_argument("--credit-line", default="")
    r.add_argument("--retry-held", action="store_true")
    r.add_argument("--no-publish", action="store_true", help="حوّل وافحص فقط دون نشر")
    r.add_argument("--template-only", action="store_true", help="بلا LLM: صفحة وقائعية، مسودة فقط")
    r.add_argument("--sleep", type=float, default=1.0)
    r.add_argument("--only-series", action=argparse.BooleanOptionalAction, default=True,
                   help="مسلسلات فقط (افتراضي)؛ --no-only-series لتشمل الأفلام")
    r.add_argument("--countries", default="مصر", help="بلد/بلدان الإنتاج المسموحة مفصولة بفاصلة؛ فارغ = كل البلدان")
    s = sub.add_parser("status")
    s.add_argument("--data-dir", default="elcinema_data")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.cmd == "status":
        cmd_status(a)
        return 0
    st = run(a)
    return 0 if st["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
