#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
elcinema_scraper.py
===================
جامع بيانات الأفلام والمسلسلات من elcinema.com (للاستخدام المرخّص).

الأوامر:
  inspect ID        جلب عمل واحد وطباعة ما تم استخراجه (للتأكد من المحلّلات)
  discover          اكتشاف كل الأعمال من فهارس السنوات (والتصنيفات اختياريًا) وإضافتها لطابور الجلب
  fetch-pending     جلب تفاصيل كل ما في الطابور (قابل للاستئناف)
  backfill          discover + fetch-pending (سحب القاعدة كاملة)
  update            اكتشاف الأعمال الجديدة فقط + جلب تفاصيلها (للتشغيل الدوري)
  schedules         تحديث مواعيد السينمات ودليل القنوات للأعمال المعروضة الآن/قريبًا
  sitemap           فحص خريطة الموقع (إن وُجدت) وعرض/إضافة الأعمال غير الموجودة عندك
  watch             تشغيل update في حلقة تلقائية كل N ثانية
  fetch --ids ...   جلب أعمال محددة
  export            تصدير إلى CSV / JSONL
  stats             إحصاءات القاعدة

كل شيء يُخزَّن في SQLite + نسخة HTML خام مضغوطة (لإعادة التحليل دون إعادة الجلب).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://elcinema.com"
DEFAULT_UA = "elcinema-licensed-collector/1.0"
log = logging.getLogger("elcinema")

WORK_RE = re.compile(r"^(?:https?://(?:www\.)?elcinema\.com)?/work/(\d+)(?:[/?#]|$)")
PERSON_RE = re.compile(r"^(?:https?://(?:www\.)?elcinema\.com)?/person/(\d+)(?:[/?#]|$)")

# صفحات تظهر فيها الأعمال الجديدة/الحالية (تُفحص في كل update)
LATEST_SOURCES = [
    "/now/", "/soon/", "/soon/gulf", "/tvguide/", "/tvsummary/",
    "/seasonals/", "/ramadan/", "/boxoffice/EG/", "/lineup/",
]

# ----------------------------------------------------------------------------
# أدوات نصية
# ----------------------------------------------------------------------------
_DIAC = re.compile(r"[\u0640\u064B-\u065F\u0670]")


def clean(s: Optional[str]) -> str:
    s = (s or "").replace("\u200f", "").replace("\u200e", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_key(s: str) -> str:
    """توحيد أسماء الحقول العربية (أشكال العرض، الهمزات، التشكيل، النقطتان)."""
    s = unicodedata.normalize("NFKC", s or "")
    s = _DIAC.sub("", s)
    s = s.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    s = s.replace("؟", "").replace("?", "")
    s = re.sub(r"[:：]\s*$", "", clean(s))
    return clean(s)


def ascii_digits(s: str) -> str:
    return s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_FIELD_SRC = {
    "إخراج": "directors", "تأليف": "writers", "طاقم العمل": "cast_top",
    "نوع العمل": "work_type", "تصنيف العمل": "genres_block", "تاريخ العرض": "release_dates",
    "التصنيف الرقابي": "age_rating", "اللغة": "languages", "بلد الإنتاج": "countries",
    "هل العمل ملون": "is_color", "مواقع التصوير": "filming_locations",
    "شباك التذاكر": "box_office", "حلقات المسلسل": "episodes_count",
    "دليل المنصات": "platforms_block", "منصات اخري": "other_platforms",
}
FIELD_MAP = {norm_key(k): v for k, v in _FIELD_SRC.items()}
LABEL_RE = re.compile(r"^[^:：]{2,40}[:：]$")

# --- ثوابت وأدوات الإضافات (مواعيد السينمات، دليل القنوات، الصور، التعليقات) ---
EXTRAS_ALL = ("showtimes", "tvguide", "gallery", "comments")
LOCKED_CMDS = {"discover", "fetch-pending", "backfill", "update", "watch", "fetch", "schedules"}
THEATER_RE = re.compile(r"^(?:https?://(?:www\.)?elcinema\.com)?/theater/(\d+)(?:[/?#]|$)")
CHANNEL_RE = re.compile(r"^(?:https?://(?:www\.)?elcinema\.com)?/tvguide/(\d+)(?:[/?#]|$)")
_MONTHS = {norm_key(k): v for k, v in {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "مايو": 5, "يونيو": 6, "يوليو": 7,
    "أغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12}.items()}
_DAY_RE = re.compile(r"(?:السبت|الاحد|الاثنين|الثلاثاء|الاربعاء|الخميس|الجمعه)\s+(\d{1,2})\s+([^\s\d]+)")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(صباحا|ظهرا|عصرا|مساء|ليلا)")


def norm_ar(s: str) -> str:
    """تطبيع نص عربي للمطابقة: أشكال العرض، التشكيل (والتنوين)، الهمزات، التاء المربوطة، الأرقام."""
    s = unicodedata.normalize("NFKC", s or "")
    s = _DIAC.sub("", s)
    s = s.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))
    return ascii_digits(clean(s))


def to_24h(h: int, m: int, period: str) -> tuple[str, bool]:
    """يحوّل وقتًا عربيًا (صباحا/ظهرا/عصرا/مساء/ليلا) إلى 24 ساعة. الثانية: هل يقع بعد منتصف الليل؟"""
    if period == "صباحا":
        hh = 0 if h == 12 else h
    elif period == "ظهرا":
        hh = h if (h == 12 or h >= 10) else h + 12
    elif period in ("عصرا", "مساء"):
        hh = h + 12 if h < 12 else h
    else:  # ليلا
        hh = 0 if h == 12 else (h if h < 6 else h + 12)
    return f"{hh:02d}:{m:02d}", (period == "ليلا" and hh < 6)


def parse_times(text: str) -> list[dict]:
    out = []
    for m in _TIME_RE.finditer(norm_ar(text)):
        h, mi, p = int(m.group(1)), int(m.group(2)), m.group(3)
        t24, nxt = to_24h(h, mi, p)
        out.append({"raw": f"{h:02d}:{mi:02d} {p}", "time_24h": t24, "next_day": nxt})
    return out


def infer_date(day: int, month: int, today: date) -> Optional[str]:
    """التاريخ المعروض بلا سنة (مثل «الخميس 5 نوفمبر») → أقرب سنة منطقية لليوم."""
    def mk(y):
        try:
            return date(y, month, day)
        except ValueError:
            return None
    cand = mk(today.year)
    if cand is None:
        return None
    delta = (cand - today).days
    if delta < -180:
        cand = mk(today.year + 1) or cand
    elif delta > 185:
        cand = mk(today.year - 1) or cand
    return cand.isoformat()


def _dedup(rows: list[dict], keyf) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        k = keyf(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out

# ----------------------------------------------------------------------------
# الجلب (احترام robots.txt + تأخير + إعادة محاولة + كاش)
# ----------------------------------------------------------------------------


class Fetcher:
    def __init__(self, delay: float, ua: str, cache_dir: Path, ignore_robots: bool = False,
                 timeout: int = 30, retries: int = 5):
        self.delay, self.ua, self.timeout, self.retries = delay, ua, timeout, retries
        self.cache_dir = cache_dir
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ua,
            "Accept-Language": "ar,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml",
        })
        self._last = 0.0
        self.heartbeat = None   # دالة تُستدعى أثناء الجلب لتحديث قفل التشغيل
        self.rp: Optional[robotparser.RobotFileParser] = None
        self.sitemaps: list[str] = []
        if not ignore_robots:
            self._load_robots()

    def _load_robots(self) -> None:
        try:
            r = self.s.get(BASE + "/robots.txt", timeout=self.timeout)
            if r.status_code == 200 and r.text.strip():
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                self.rp = rp
                self.sitemaps = list(rp.site_maps() or [])
                cd = rp.crawl_delay(self.ua) or rp.crawl_delay("*")
                if cd:
                    self.delay = max(self.delay, float(cd))
                    log.info("robots.txt crawl-delay=%s → delay=%.1fs", cd, self.delay)
            else:
                log.info("robots.txt: HTTP %s (لا قيود مقروءة)", r.status_code)
        except requests.RequestException as e:
            log.warning("تعذّر قراءة robots.txt: %s", e)

    def allowed(self, url: str) -> bool:
        return self.rp is None or self.rp.can_fetch(self.ua, url)

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.html.gz"

    def get(self, url: str, use_cache: bool = True) -> tuple[int, Optional[str]]:
        """يعيد (status, html). 200 نجاح، 404 غير موجود، 403 ممنوع بـ robots، 0 فشل."""
        if not self.allowed(url):
            log.warning("robots.txt يمنع: %s", url)
            return 403, None
        cp = self._cache_path(url)
        if use_cache and cp.exists():
            with gzip.open(cp, "rt", encoding="utf-8") as fh:
                return 200, fh.read()
        for attempt in range(1, self.retries + 1):
            if self.heartbeat:
                self.heartbeat()
            wait = self.delay + random.uniform(0, self.delay * 0.3) - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as e:
                log.warning("%s (محاولة %d): %s", url, attempt, e)
                self._last = time.monotonic()
                time.sleep(min(60, 2 ** attempt))
                continue
            self._last = time.monotonic()
            if r.status_code == 200:
                r.encoding = "utf-8"
                cp.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(cp, "wt", encoding="utf-8") as fh:
                    fh.write(r.text)
                return 200, r.text
            if r.status_code in (404, 410):
                return 404, None
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After", "")
                sleep = int(ra) if ra.isdigit() else min(300, 5 * 2 ** attempt)
                log.warning("HTTP %s من %s — انتظار %ds", r.status_code, url, sleep)
                time.sleep(sleep)
                continue
            log.warning("HTTP %s لـ %s", r.status_code, url)
            return r.status_code, None
        return 0, None

    def _wait(self) -> None:
        wait = self.delay + random.uniform(0, self.delay * 0.3) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def download(self, url: str, dest: Path) -> bool:
        """تنزيل صورة (من نطاقات elcinema.com فقط). يعيد True إن وُجد الملف أو نُزِّل."""
        if dest.exists():
            return True
        host = urlparse(url).netloc.lower()
        if not (host == "elcinema.com" or host.endswith(".elcinema.com")) or not self.allowed(url):
            return False
        self._wait()
        try:
            r = self.s.get(url, timeout=self.timeout, stream=True)
        except requests.RequestException as e:
            log.warning("تعذّر تنزيل %s: %s", url, e)
            self._last = time.monotonic()
            return False
        self._last = time.monotonic()
        if r.status_code != 200 or "image" not in r.headers.get("Content-Type", ""):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        return True

    def get_bytes(self, url: str) -> Optional[bytes]:
        """جلب خام (للـ sitemap بما فيه .gz). يعيد None عند عدم الوجود أو الفشل."""
        if not self.allowed(url):
            return None
        for attempt in range(1, 4):
            self._wait()
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("%s: %s", url, e)
                self._last = time.monotonic()
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            if r.status_code == 200:
                return r.content
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * 2 ** attempt)
                continue
            return None
        return None

    def invalidate(self, url: str) -> None:
        """يحذف نسخة الكاش (حتى لا تبقى صفحة غير صالحة لمعرّف قد يصبح عملًا حقيقيًا لاحقًا)."""
        try:
            self._cache_path(url).unlink()
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------------------
# قاعدة البيانات
# ----------------------------------------------------------------------------
WORK_COLS = [
    "work_id", "url", "title_ar", "title_alt", "year", "category_id", "category_label",
    "subcategory", "is_series", "rating", "votes", "duration_min", "status", "poster_url",
    "trailer_url", "synopsis", "age_rating", "is_color", "countries", "languages", "genres",
    "release_dates", "directors", "writers", "cast_top", "filming_locations",
    "box_office_text", "box_office_value", "episodes_count", "watch_links", "header_items", "extra",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
  work_id INTEGER PRIMARY KEY, source TEXT, first_seen_at TEXT,
  state TEXT DEFAULT 'pending', hint_title TEXT, hint_category TEXT, hint_year TEXT,
  attempts INTEGER DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS works (
  work_id INTEGER PRIMARY KEY, url TEXT, title_ar TEXT, title_alt TEXT, year INTEGER,
  category_id INTEGER, category_label TEXT, subcategory TEXT, is_series INTEGER,
  rating REAL, votes INTEGER, duration_min INTEGER, status TEXT, poster_url TEXT,
  trailer_url TEXT, synopsis TEXT, age_rating TEXT, is_color INTEGER, countries TEXT,
  languages TEXT, genres TEXT, release_dates TEXT, directors TEXT, writers TEXT,
  cast_top TEXT, filming_locations TEXT, box_office_text TEXT, box_office_value INTEGER,
  episodes_count INTEGER, watch_links TEXT, header_items TEXT, extra TEXT,
  first_seen_at TEXT, fetched_at TEXT, content_hash TEXT);
CREATE TABLE IF NOT EXISTS credits (
  work_id INTEGER, person_id INTEGER, person_name TEXT, section TEXT, role TEXT,
  PRIMARY KEY (work_id, person_id, section, role));
CREATE TABLE IF NOT EXISTS episodes (
  work_id INTEGER, episode_no INTEGER, summary TEXT, PRIMARY KEY (work_id, episode_no));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cmd TEXT, started_at TEXT, finished_at TEXT, ok INTEGER);
CREATE INDEX IF NOT EXISTS idx_works_year ON works(year);
CREATE INDEX IF NOT EXISTS idx_works_cat ON works(category_id);
CREATE TABLE IF NOT EXISTS showtimes (
  work_id INTEGER, captured_at TEXT, show_date TEXT, theater_id INTEGER, theater_name TEXT,
  times TEXT, booking_url TEXT, PRIMARY KEY (work_id, show_date, theater_id));
CREATE TABLE IF NOT EXISTS tv_airings (
  work_id INTEGER, captured_at TEXT, channel_id INTEGER, channel_name TEXT, air_date TEXT,
  date_text TEXT, time_raw TEXT, time_24h TEXT, subscribe_url TEXT,
  PRIMARY KEY (work_id, channel_id, date_text, time_raw));
CREATE TABLE IF NOT EXISTS gallery (
  work_id INTEGER, image_id INTEGER, page_url TEXT, thumb_url TEXT, full_url_guess TEXT,
  caption TEXT, local_path TEXT, PRIMARY KEY (work_id, image_id));
CREATE TABLE IF NOT EXISTS comments (
  work_id INTEGER, kind TEXT, comment_id TEXT, author TEXT, author_url TEXT, date_text TEXT,
  text TEXT, title TEXT, helpful INTEGER, PRIMARY KEY (work_id, kind, comment_id));
"""


class RunLock:
    """قفل ملف بين العمليات: يمنع تداخل التشغيلات المجدولة. يُعدّ قديمًا (عالقًا) إن لم يُحدَّث لمدة stale ثانية."""

    def __init__(self, path: Path, stale: int = 1800):
        self.path, self.stale, self._t = Path(path), stale, 0.0

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale:
                        self.path.unlink()      # بقايا تشغيل انهار؛ نأخذ القفل
                        continue
                except OSError:
                    pass
                return False
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            return True
        return False

    def touch(self) -> None:
        if time.time() - self._t > 20:
            self._t = time.time()
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class DB:
    def __init__(self, path: Path):
        self.c = sqlite3.connect(path, timeout=60)
        try:
            self.c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.c.executescript(SCHEMA)
        for col, typ in (("title", "TEXT"), ("helpful", "INTEGER")):   # ترقية قواعد أُنشئت بنسخ أقدم
            try:
                self.c.execute(f"ALTER TABLE comments ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        self.c.commit()

    def log_run(self, cmd: str, started_at: str, ok: bool) -> None:
        self.c.execute("INSERT INTO runs(cmd, started_at, finished_at, ok) VALUES (?,?,?,?)",
                       (cmd, started_at, now_iso(), 1 if ok else 0))
        self.c.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 2000)")
        self.c.commit()

    def enqueue(self, work_id: int, source: str, hint: Optional[dict] = None) -> bool:
        """True إذا كان المعرّف جديدًا تمامًا (لا في الطابور ولا في works)."""
        hint = hint or {}
        cur = self.c.execute(
            "INSERT OR IGNORE INTO queue(work_id, source, first_seen_at, hint_title, hint_category, hint_year) "
            "VALUES (?,?,?,?,?,?)",
            (work_id, source, now_iso(), hint.get("title"), hint.get("category"), hint.get("year")))
        return cur.rowcount == 1

    def pending_ids(self, limit: Optional[int] = None) -> list[int]:
        q = "SELECT work_id FROM queue WHERE state IN ('pending','error') AND attempts < 5 ORDER BY work_id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [r[0] for r in self.c.execute(q)]

    def mark(self, work_id: int, state: str, err: Optional[str] = None) -> None:
        self.c.execute(
            "INSERT INTO queue(work_id, source, first_seen_at, state, last_error, attempts) "
            "VALUES (?, 'direct', ?, ?, ?, ?) "
            "ON CONFLICT(work_id) DO UPDATE SET state=excluded.state, last_error=excluded.last_error, "
            "attempts=queue.attempts+excluded.attempts",
            (work_id, now_iso(), state, err, 1 if state == "error" else 0))
        self.c.commit()

    def save_work(self, d: dict, credits: list[dict], episodes: list[dict]) -> bool:
        """يحفظ العمل. يعيد True إذا كان جديدًا أو تغيّر محتواه."""
        row = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
               for k, v in d.items() if k in WORK_COLS}
        chash = hashlib.sha1(json.dumps([d, credits, episodes], sort_keys=True,
                                        ensure_ascii=False, default=str).encode()).hexdigest()
        old = self.c.execute("SELECT content_hash FROM works WHERE work_id=?", (d["work_id"],)).fetchone()
        ts = now_iso()
        cols = list(row.keys()) + ["first_seen_at", "fetched_at", "content_hash"]
        vals = list(row.values()) + [ts, ts, chash]
        upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "first_seen_at")
        self.c.execute(
            f"INSERT INTO works({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(work_id) DO UPDATE SET {upd}", vals)
        self.c.execute("DELETE FROM credits WHERE work_id=?", (d["work_id"],))
        self.c.executemany(
            "INSERT OR IGNORE INTO credits VALUES (?,?,?,?,?)",
            [(d["work_id"], c["person_id"], c["name"], c.get("section"), c.get("role")) for c in credits])
        self.c.execute("DELETE FROM episodes WHERE work_id=?", (d["work_id"],))
        self.c.executemany(
            "INSERT OR IGNORE INTO episodes VALUES (?,?,?)",
            [(d["work_id"], e["episode_no"], e["summary"]) for e in episodes])
        self.c.commit()
        return old is None or old[0] != chash

    def save_extras(self, work_id: int, ex: dict) -> None:
        """يحفظ الإضافات الموجودة في ex فقط. المواعيد والدليل لقطات تُستبدل؛ الصور والتعليقات تُدمج."""
        ts = now_iso()
        c = self.c
        if "showtimes" in ex:
            c.execute("DELETE FROM showtimes WHERE work_id=?", (work_id,))
            c.executemany(
                "INSERT OR REPLACE INTO showtimes VALUES (?,?,?,?,?,?,?)",
                [(work_id, ts, s.get("date") or "", s["theater_id"], s["theater_name"],
                  json.dumps(s["times"], ensure_ascii=False), s.get("booking_url")) for s in ex["showtimes"]])
        if "tvguide" in ex:
            c.execute("DELETE FROM tv_airings WHERE work_id=?", (work_id,))
            c.executemany(
                "INSERT OR REPLACE INTO tv_airings VALUES (?,?,?,?,?,?,?,?,?)",
                [(work_id, ts, t["channel_id"], t["channel_name"], t.get("date"), t.get("date_text") or "",
                  t["time_raw"], t["time_24h"], t.get("subscribe_url")) for t in ex["tvguide"]])
        for g in ex.get("gallery", []):
            c.execute(
                "INSERT INTO gallery(work_id, image_id, page_url, thumb_url, full_url_guess, caption) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(work_id, image_id) DO UPDATE SET page_url=excluded.page_url, "
                "thumb_url=excluded.thumb_url, full_url_guess=excluded.full_url_guess, caption=excluded.caption",
                (work_id, g["image_id"], g["page_url"], g["thumb_url"], g["full_url_guess"], g["caption"]))
        for m in ex.get("comments", []):
            c.execute(
                "INSERT INTO comments(work_id, kind, comment_id, author, author_url, date_text, text, title, helpful) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(work_id, kind, comment_id) DO UPDATE SET "
                "author=excluded.author, author_url=excluded.author_url, date_text=excluded.date_text, "
                "text=excluded.text, title=excluded.title, helpful=excluded.helpful",
                (work_id, m["kind"], m["comment_id"], m["author"], m["author_url"], m["date_text"], m["text"],
                 m.get("title"), m.get("helpful")))
        c.commit()


# ----------------------------------------------------------------------------
# التحليل (parsing)
# ----------------------------------------------------------------------------
def to_abs(href: str) -> str:
    return urljoin(BASE + "/", href)


def extract_work_ids(html: str) -> list[int]:
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = WORK_RE.match(a["href"])
        if m and int(m.group(1)) not in seen:
            seen.add(int(m.group(1)))
            out.append(int(m.group(1)))
    return out


def parse_listing(html: str) -> tuple[list[dict], int]:
    """صفحة فهرس: يعيد (الصفوف, أعلى رقم صفحة ظاهر في الترقيم)."""
    soup = BeautifulSoup(html, "lxml")
    rows, seen = [], set()
    for tr in soup.find_all("tr"):
        a = next((c for c in tr.find_all("a", href=WORK_RE) if clean(c.get_text())), None)
        if not a:
            continue
        wid = int(WORK_RE.match(a["href"]).group(1))
        if wid in seen:
            continue
        seen.add(wid)
        tds = tr.find_all("td")
        cells = [clean(td.get_text(" ")) for td in tds]
        idx = next((i for i, td in enumerate(tds) if a in td.find_all("a")), 1)
        g = lambda k: cells[idx + k] if len(cells) > idx + k else None  # noqa: E731
        rows.append({"work_id": wid, "title": clean(a.get_text()),
                     "category": g(1), "subcategory": g(2), "year": g(3)})
    if not rows:  # احتياط: أي روابط أعمال في الصفحة
        for wid in extract_work_ids(html):
            rows.append({"work_id": wid, "title": None, "category": None, "subcategory": None, "year": None})
    maxp = 1
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            maxp = max(maxp, int(m.group(1)))
    return rows, maxp


def _value_of(el) -> dict:
    return {"text": clean(el.get_text(" ")),
            "links": [{"text": clean(a.get_text(" ")), "href": to_abs(a["href"])}
                      for a in el.find_all("a", href=True)]}


def extract_pairs(soup: BeautifulSoup) -> dict[str, list[dict]]:
    """أزواج (حقل: قيم) من عناصر li/dt/th التي نصها 'عنوان:' ثم الأشقاء التالية."""
    def is_label(node) -> bool:
        if node.find(["a", "ul"]):
            return False
        t = clean(node.get_text(" "))
        # بعض العناوين تظهر بلا نقطتين (مثل «مواقع التصوير») فنقبل أيضًا المعروف منها
        return bool(LABEL_RE.match(t)) or (len(t) <= 40 and norm_key(t) in FIELD_MAP)

    pairs: dict[str, list[dict]] = {}
    for el in soup.find_all(["li", "dt", "th"]):
        if not is_label(el):
            continue
        txt = clean(el.get_text(" "))
        values = []
        for sib in el.find_next_siblings():
            stxt = clean(sib.get_text(" "))
            if sib.name == el.name and is_label(sib):
                break
            if stxt:
                values.append(_value_of(sib))
        key = norm_key(txt)
        if key not in pairs or len(values) > len(pairs[key]):
            pairs[key] = values
    return pairs


def people_from_values(values: list[dict], section: Optional[str] = None) -> list[dict]:
    out = []
    for v in values:
        for l in v["links"]:
            m = PERSON_RE.match(l["href"])
            if not m or not l["text"]:
                continue
            rest = v["text"].replace(l["text"], "", 1)
            rm = re.search(r"\(([^()]+)\)", rest)
            role = clean(rm.group(1)) if rm else None
            if role == "المزيد":
                role = None
            out.append({"person_id": int(m.group(1)), "name": l["text"], "role": role, "section": section})
    return out


def parse_release_dates(values: list[dict]) -> list[dict]:
    out = []
    for v in values:
        m = re.match(r"^(.*?)\s*\[\s*(.+?)\s*\]\s*$", v["text"])
        country = clean(m.group(1)) if m else None
        date_text = clean(m.group(2)) if m else v["text"]
        md = yr = None
        for l in v["links"]:
            md = md or re.search(r"release_day/(\d{2})-(\d{2})", l["href"])
            yr = yr or re.search(r"release_year/(\d{3,4})", l["href"])
        iso = f"{int(yr.group(1)):04d}-{md.group(1)}-{md.group(2)}" if (md and yr) else None
        out.append({"country": country, "date_text": date_text, "date": iso})
    return out


def _uniq_links(soup: BeautifulSoup, pattern: str) -> list[dict]:
    rx = re.compile(pattern)
    out, seen = [], set()
    for a in soup.find_all("a", href=rx):
        txt = clean(a.get_text(" "))
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append({"id": rx.search(a["href"]).group(1), "name": txt})
    return out


def _meta(soup: BeautifulSoup, key: str) -> Optional[str]:
    t = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return t.get("content") if t and t.get("content") else None


_STATUS_SKIP = re.compile(r"^\d+\s*دقيقة$")


def parse_work(html: str, work_id: int) -> Optional[dict]:
    soup = BeautifulSoup(html, "lxml")
    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href") and f"/work/{work_id}" not in canon["href"]:
        return None
    h1 = soup.find("h1")
    if not h1:
        return None
    h1_text = clean(h1.get_text(" "))
    title_ar, year, title_alt = h1_text, None, None
    m = re.match(r"^(.*?)\s*\((\d{3,4})\)\s*(.*)$", h1_text)
    if m:
        title_ar, year, title_alt = clean(m.group(1)), int(m.group(2)), (clean(m.group(3)) or None)

    d: dict[str, Any] = {"work_id": work_id, "url": f"{BASE}/work/{work_id}/",
                         "title_ar": title_ar, "title_alt": title_alt, "year": year}

    # التصنيف والترويسة
    cat_a = soup.find("a", href=re.compile(r"/index/work/category/\d+"))
    d["category_id"] = int(re.search(r"category/(\d+)", cat_a["href"]).group(1)) if cat_a else None
    d["category_label"] = clean(cat_a.get_text(" ")) if cat_a else None
    sub_a = soup.find("a", href=re.compile(r"/index/work/sub_category/"))
    d["subcategory"] = clean(sub_a.get_text(" ")) if sub_a else None
    d["is_series"] = int(d["category_id"] == 3 or "مسلسل" in (d["category_label"] or ""))

    ul = cat_a.find_parent(["ul", "ol"]) if cat_a else None
    header_items = [clean(li.get_text(" ")) for li in ul.find_all("li", recursive=False)] if ul else []
    d["header_items"] = header_items
    d["duration_min"] = None
    for it in header_items:
        dm = re.match(r"^(\d+)\s*دقيقة$", ascii_digits(it))
        if dm:
            d["duration_min"] = int(dm.group(1))
    countries = _uniq_links(soup, r"/index/work/country/([^/?#]+)")
    cnames = {c["name"] for c in countries}
    d["status"] = next((it for it in header_items
                        if it and it != d["category_label"] and it not in cnames
                        and not _STATUS_SKIP.match(ascii_digits(it))), None)

    # التقييم
    d["rating"], d["votes"] = None, None
    st = soup.find("a", href=re.compile(r"/work/\d+/stats"))
    if st:
        rm = re.search(r"([\d.]+)\s*-\s*عدد\s*(\d+)", ascii_digits(st.get("title", "")))
        try:
            val = float(ascii_digits(clean(st.get_text())))
            d["rating"] = val if val > 0 else None
        except ValueError:
            pass
        if rm:
            d["votes"] = int(rm.group(2))

    d["poster_url"] = _meta(soup, "og:image")
    tr = soup.find("a", href=re.compile(r"/work/\d+/video/\d+"))
    d["trailer_url"] = to_abs(tr["href"]) if tr else None

    # القصة (النص الكامل موجود في الصفحة ويُقطَع بزر «اقرأ المزيد»)
    d["synopsis"] = None
    for stg in soup.find_all(["strong", "b"]):
        if norm_key(stg.get_text()) == norm_key("ملخص القصة"):
            # الصفحة الحقيقية: <strong>ملخص القصة:</strong> <p>نص<a>اقرأ المزيد</a><span class="hide">تكملة</span></p>
            para = stg.find_next_sibling("p")
            target = para if para is not None else stg.parent
            for a in target.find_all("a"):
                if "اقرأ المزيد" in a.get_text():
                    a.decompose()
            text = clean(target.get_text(" "))
            if para is None:
                text = clean(text.replace(clean(stg.get_text(" ")), "", 1))
            d["synopsis"] = text or None
            break

    # الحقول المعنونة
    pairs = extract_pairs(soup)
    extra: dict[str, str] = {}
    mapped: dict[str, list[dict]] = {}
    for k, vals in pairs.items():
        if k in FIELD_MAP:
            mapped[FIELD_MAP[k]] = vals
        else:
            extra[k] = " | ".join(v["text"] for v in vals)

    d["directors"] = people_from_values(mapped.get("directors", []), "إخراج")
    d["writers"] = people_from_values(mapped.get("writers", []), "تأليف")
    d["cast_top"] = people_from_values(mapped.get("cast_top", []), "طاقم العمل")
    d["genres"] = _uniq_links(soup, r"/index/work/genre/(\d+)")
    d["languages"] = _uniq_links(soup, r"/index/work/language/([^/?#]+)")
    d["countries"] = countries
    d["release_dates"] = parse_release_dates(mapped.get("release_dates", []))
    d["age_rating"] = " ".join(v["text"] for v in mapped.get("age_rating", [])) or None
    col = " ".join(v["text"] for v in mapped.get("is_color", []))
    d["is_color"] = 1 if col.startswith("نعم") else (0 if col.startswith("لا") else None)
    d["filming_locations"] = [v["text"] for v in mapped.get("filming_locations", [])]
    bo = " ".join(v["text"] for v in mapped.get("box_office", []))
    d["box_office_text"] = clean(bo.replace("(المزيد)", "")) or None
    bm = re.search(r"([\d,]{3,})", ascii_digits(bo))
    d["box_office_value"] = int(bm.group(1).replace(",", "")) if bm else None
    ec = " ".join(v["text"] for v in mapped.get("episodes_count", []))
    em = re.search(r"(\d+)\s*حلقة", ascii_digits(ec))
    if not em:
        em = re.search(r"\((\d+)\s*حلقة\)", ascii_digits(soup.get_text(" ")))
    d["episodes_count"] = int(em.group(1)) if em else None

    # روابط المشاهدة الخاصة بالعمل (تلك التي تسبق قسم «مشاهدة اونلاين» المخصّص لأعمال أخرى)
    skip = ("elcinema.com", "facebook.com", "x.com", "twitter.com", "instagram.com",
            "googletagmanager.com", "play.google.com", "apps.apple.com")
    stop = next((h for h in soup.find_all(["h2", "h3", "h4"])
                 if clean(h.get_text()) == "مشاهدة اونلاين"), None)
    anchors = reversed(stop.find_all_previous("a", href=True)) if stop else soup.find_all("a", href=True)
    watch, wseen = [], set()
    for a in anchors:
        href = a["href"]
        if not href.startswith("http") or any(s in href for s in skip) or href in wseen:
            continue
        wseen.add(href)
        watch.append({"text": clean(a.get_text(" ")), "title": a.get("title"), "href": href})
    d["watch_links"] = watch
    d["extra"] = extra
    d["_has_episodes_page"] = bool(soup.find("a", href=re.compile(rf"/work/{work_id}/episodes")))
    for name in ("theater", "tvguide", "gallery"):
        d[f"_has_{name}"] = bool(soup.find("a", href=re.compile(rf"/work/{work_id}/{name}(?:[/?#]|$)")))
    d["_review_links"] = sorted({to_abs(a["href"]) for a in soup.find_all(
        "a", href=re.compile(rf"/work/{work_id}/(?:reviews?|critics?)(?:[/?#]|$)"))})
    return d


def parse_people(html: str) -> list[dict]:
    """صفحة الطاقم الكاملة: كل روابط /person/ مع القسم والدور."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=PERSON_RE):
        name = clean(a.get_text(" "))
        if not name:
            continue
        pid = int(PERSON_RE.match(a["href"]).group(1))
        cont = a.find_parent(["li", "tr"]) or a.parent
        rest = clean(cont.get_text(" ")).replace(name, "", 1)
        rm = re.search(r"\(([^()]+)\)", rest)
        role = clean(rm.group(1)) if rm else None
        if role == "المزيد":
            role = None
        head = a.find_previous(re.compile(r"^h[2-6]$"))
        section = clean(head.get_text(" ")) if head else None
        key = (pid, section, role)
        if key in seen:
            continue
        seen.add(key)
        out.append({"person_id": pid, "name": name, "role": role, "section": section})
    return out


_EP_RE = re.compile(r"^حلقة\s*#?\s*(\d+)\s*[:：]?\s*(.*)$", re.S)


def parse_episodes(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    eps: dict[int, str] = {}
    for li in soup.find_all("li"):
        txt = clean(ascii_digits(li.get_text(" ")))
        if len(re.findall(r"حلقة\s*#?\s*\d+\s*[:：]", txt)) > 1:
            continue  # حاوية لعدة حلقات
        m = _EP_RE.match(txt)
        if m:
            eps.setdefault(int(m.group(1)), clean(m.group(2)))
    if not eps:  # احتياط: تقسيم النص
        body = clean(ascii_digits(soup.get_text(" ")))
        body = body.split("حقوق الطبع")[0]
        for chunk in re.split(r"(?=حلقة\s*#?\s*\d+\s*[:：])", body):
            m = _EP_RE.match(clean(chunk))
            if m:
                eps.setdefault(int(m.group(1)), clean(m.group(2)))
    return [{"episode_no": n, "summary": s} for n, s in sorted(eps.items())]


# ----------------------------------------------------------------------------
# الاكتشاف
# ----------------------------------------------------------------------------
def _cinema_container(a):
    """أصغر حاوية للرابط تحوي أوقات عرض ولا تحوي أكثر من سينما واحدة."""
    node = a.parent
    while node is not None and getattr(node, "name", None) not in (None, "body", "html", "[document]"):
        ids = {THEATER_RE.match(x["href"]).group(1) for x in node.find_all("a", href=THEATER_RE)}
        if len(ids) > 1:
            return None
        if parse_times(node.get_text(" ")):
            return node
        node = node.parent
    return None


def parse_showtimes(html: str) -> list[dict]:
    """مواعيد العرض في السينمات: لكل (يوم، سينما) قائمة أوقات (خام + 24 ساعة)."""
    soup = BeautifulSoup(html, "lxml")
    blocks = []
    for el in soup.find_all(id=re.compile(r"^wtheater\d{8}$")):
        s = el["id"][-8:]
        blocks.append((f"{s[:4]}-{s[4:6]}-{s[6:]}", el))
    if not blocks:
        blocks = [(None, soup)]
    out, seen = [], set()
    for day, root in blocks:
        for a in root.find_all("a", href=THEATER_RE):
            text = clean(a.get_text(" "))
            if not text:
                continue
            tid = int(THEATER_RE.match(a["href"]).group(1))
            if (day, tid) in seen:
                continue
            cont = _cinema_container(a)
            if cont is None:
                continue
            seen.add((day, tid))
            book = cont.find("a", href=re.compile(r"/booking/"))
            out.append({"date": day, "theater_id": tid, "theater_name": clean(a.get("title") or text),
                        "times": parse_times(cont.get_text(" ")),
                        "booking_url": to_abs(book["href"]) if book else None})
    return out


def parse_tvguide(html: str, today: Optional[date] = None) -> list[dict]:
    """جدول عرض العمل على القنوات: قناة + تاريخ + وقت."""
    today = today or date.today()
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for tr in soup.find_all("tr"):
        a = next((x for x in tr.find_all("a", href=CHANNEL_RE) if clean(x.get_text())), None)
        if not a:
            continue
        text = norm_ar(tr.get_text(" "))
        times = parse_times(text)
        if not times:
            continue
        dm = _DAY_RE.search(text)
        iso = None
        if dm:
            mon = _MONTHS.get(norm_ar(dm.group(2)))
            if mon:
                iso = infer_date(int(dm.group(1)), mon, today)
        ext = next((x["href"] for x in tr.find_all("a", href=True)
                    if x["href"].startswith("http") and "elcinema.com" not in x["href"]), None)
        cid = int(CHANNEL_RE.match(a["href"]).group(1))
        key = (cid, dm.group(0) if dm else "", times[0]["raw"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"channel_id": cid, "channel_name": clean(a.get_text(" ")), "date": iso,
                    "date_text": dm.group(0) if dm else "", "time_raw": times[0]["raw"],
                    "time_24h": times[0]["time_24h"], "subscribe_url": ext})
    return out


def parse_gallery(html: str, work_id: int) -> list[dict]:
    """روابط الصور (معرّف الصورة، صفحتها، المصغّرة، ورابط الأصل المرجّح بحذف بادئة المقاس)."""
    soup = BeautifulSoup(html, "lxml")
    rx = re.compile(rf"/work/{work_id}/gallery/(\d+)")
    out, seen = [], set()
    for a in soup.find_all("a", href=rx):
        iid = int(rx.search(a["href"]).group(1))
        if iid in seen:
            continue
        seen.add(iid)
        img = a.find("img")
        thumb = None
        if img:
            for attr in ("data-src", "data-original", "data-lazy-src", "src"):
                v = img.get(attr)
                if v and not v.startswith("data:"):
                    thumb = to_abs(v)
                    break
        guess = re.sub(r"/_\d*x\d*_", "/", thumb) if thumb else None
        cap = clean((img.get("alt") if img else "") or a.get("title") or "")
        out.append({"image_id": iid, "page_url": to_abs(a["href"]), "thumb_url": thumb,
                    "full_url_guess": guess, "caption": cap or None})
    return out


_REVIEW_RE = re.compile(r"^(?:https?://(?:www\.)?elcinema\.com)?/review/(\d+)(?:[/?#]|$)")
_USER_RE = re.compile(r"/user/\d+")


def parse_reviews(html: str) -> list[dict]:
    """صفحة «أراء حرة» (/work/ID/review): كل مراجعة = h4 برابط /review/ID/ ثم كاتب وتاريخ ثم النص."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for h4 in soup.find_all("h4"):
        link = h4.find("a", href=_REVIEW_RE)
        if not link:
            continue
        rid = _REVIEW_RE.match(link["href"]).group(1)
        if rid in seen:
            continue
        seen.add(rid)
        author = author_url = date_text = body = None
        helpful = None
        for sib in h4.find_next_siblings():
            if sib.name in ("h4", "hr", "h1", "h2", "h3") or (sib.name == "div" and sib.get("class")
                                                             and "footer" in sib.get("class")):
                break
            if sib.name == "ul":
                for li in sib.find_all("li"):
                    a = li.find("a", href=_USER_RE)
                    t = clean(li.get_text(" "))
                    if a and author is None and "#review" not in a["href"]:
                        author, author_url = clean(a.get_text(" ")), to_abs(a["href"])
                    elif date_text is None and re.search(r"\d{4}", ascii_digits(t)) and not a:
                        date_text = t
                    hm = re.match(r"^(\d+)\s*مفيد$", ascii_digits(t))
                    if hm:
                        helpful = int(hm.group(1))
            elif sib.name == "p" and "subheader" not in (sib.get("class") or []) and body is None:
                for a in sib.find_all("a"):
                    if "اقرأ المزيد" in a.get_text():
                        a.decompose()
                body = clean(sib.get_text(" ")) or None
        out.append({"kind": "review", "comment_id": rid, "author": author, "author_url": author_url,
                    "date_text": date_text, "text": body, "title": clean(h4.get_text(" ")),
                    "helpful": helpful})
    return out


def parse_comments(html: str, kind: str = "comment") -> list[dict]:
    """تعليقات/مراجعات من HTML. تعتمد على أن العناصر المتكررة بنفس الصنف هي التعليقات.
    إن كانت التعليقات تُحمَّل بـ JavaScript فلن تظهر هنا (راجع --comments-url في README)."""
    soup = BeautifulSoup(html, "lxml")
    cls = re.compile(r"comment|review", re.I) if kind == "review" else re.compile(r"comment", re.I)
    nodes = soup.find_all(attrs={"class": cls})
    sig = lambda n: (id(n.parent), tuple(n.get("class", [])))  # noqa: E731
    counts: dict = {}
    for n in nodes:
        counts[sig(n)] = counts.get(sig(n), 0) + 1
    items = [n for n in nodes if counts[sig(n)] >= 2 and not n.find(["form", "textarea"])]
    if not items:
        ids = {id(n) for n in nodes}
        outer = [n for n in nodes if not any(id(p) in ids for p in n.parents)]
        items = [n for n in outer if not n.find(["form", "textarea"]) and len(clean(n.get_text())) > 15]
    out, seen = [], set()
    for n in items:
        txt = clean(n.get_text(" "))
        if len(txt) < 5:
            continue
        a = n.find("a", href=True)
        author = clean(a.get_text(" ")) if a else None
        dm = re.search(r"(منذ\s+[^\d]*\d*\s*\S+|\d{1,2}\s+\S+\s+\d{4})", txt)
        digits = re.search(r"(\d+)", n.get("id") or "")
        cid = digits.group(1) if digits else hashlib.sha1(((author or "") + txt[:200]).encode()).hexdigest()[:12]
        if (kind, cid) in seen:
            continue
        seen.add((kind, cid))
        out.append({"kind": kind, "comment_id": cid, "author": author,
                    "author_url": to_abs(a["href"]) if a else None,
                    "date_text": clean(dm.group(1)) if dm else None, "text": txt})
    return out


def crawl_listing(f: Fetcher, base_url: str, max_pages: int = 1000) -> Iterable[dict]:
    page, last, seen = 1, 1, set()
    while page <= min(last, max_pages):
        status, html = f.get(f"{base_url.rstrip('/')}?page={page}", use_cache=False)
        if status != 200 or not html:
            log.warning("توقف الفهرس %s عند الصفحة %d (status=%s)", base_url, page, status)
            break
        rows, maxp = parse_listing(html)
        last = max(last, maxp)
        fresh = [r for r in rows if r["work_id"] not in seen]
        if not fresh:
            break
        for r in fresh:
            seen.add(r["work_id"])
            yield r
        log.info("  %s  صفحة %d/%s  (+%d)", base_url.replace(BASE, ""), page, last, len(fresh))
        page += 1


def discover_years(f: Fetcher) -> list[int]:
    status, html = f.get(f"{BASE}/index/work/release_year", use_cache=False)
    years: set[int] = set()
    if status == 200 and html:
        for m in re.finditer(r"/index/work/release_year/(\d+)", html):
            years.add(int(m.group(1)))
    if not years:
        years = set(range(1895, datetime.now().year + 5))
    return sorted(years, reverse=True)


# ----------------------------------------------------------------------------
# المعالجة
# ----------------------------------------------------------------------------
class Ctx:
    def __init__(self, args):
        data = Path(args.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        self.args = args
        self.f = Fetcher(args.delay, args.user_agent, data / "html_cache", args.ignore_robots)
        self.db = DB(data / "elcinema.sqlite")
        self.data_dir = data
        self.last_status = 0


def _fetch_paged(ctx: Ctx, url: str, use_cache: bool, max_pages: int = 50) -> list[str]:
    htmls, seen_pages = [], set()
    page = 1
    while page <= max_pages:
        u = url if page == 1 else f"{url}?page={page}"
        status, html = ctx.f.get(u, use_cache=use_cache)
        if page == 1:
            ctx.last_status = status  # يميّز «غير موجود» (404) عن فشل مؤقت
        if status != 200 or not html:
            break
        htmls.append(html)
        nxt = set(int(x) for x in re.findall(rf"{re.escape(url.replace(BASE, ''))}\?page=(\d+)", html))
        if not (nxt - seen_pages - {page}) or max(nxt, default=0) <= page:
            break
        seen_pages.add(page)
        page += 1
    return htmls


def year_bounds(a) -> tuple[int, int]:
    return int(getattr(a, "min_year", 0) or 0), int(getattr(a, "max_year", 0) or 0)


def sitemap_filter(items: list[tuple[int, Optional[str]]], min_year: int) -> list[tuple[int, Optional[str]]]:
    """يُسقط من الـ sitemap ما تاريخ تعديله (lastmod) قبل بداية النطاق. الأعمال بلا lastmod تبقى."""
    if not min_year:
        return items
    cutoff = f"{min_year}-01-01"
    return [(w, lm) for w, lm in items if not lm or lm[:10] >= cutoff]


def process_work(ctx: Ctx, work_id: int, refresh: bool = False) -> Optional[dict]:
    a = ctx.args
    use_cache = not refresh
    ctx.last_state = "missing"
    status, html = ctx.f.get(f"{BASE}/work/{work_id}/", use_cache=use_cache)
    if status == 404:
        ctx.db.mark(work_id, "missing")
        return None
    if status != 200 or not html:
        ctx.last_state = "error"
        ctx.db.mark(work_id, "error", f"http {status}")
        return None
    d = parse_work(html, work_id)
    if not d:
        ctx.f.invalidate(f"{BASE}/work/{work_id}/")
        ctx.db.mark(work_id, "missing", "not a work page")
        return None
    mn, mx = year_bounds(a)
    yr = d.get("year")
    if yr is not None and ((mn and yr < mn) or (mx and yr > mx)):
        ctx.last_state = "skipped"          # خارج النطاق: لا نجلب الطاقم/الحلقات/الإضافات ولا نحفظه
        ctx.db.mark(work_id, "skipped", f"year {yr}")
        return None
    ctx.last_state = "ok"
    flags = {k: d.pop(k) for k in list(d) if k.startswith("_")}
    has_eps = flags.get("_has_episodes_page", False)

    credits: list[dict] = []
    if not a.no_cast:
        for h in _fetch_paged(ctx, f"{BASE}/work/{work_id}/cast", use_cache):
            credits += parse_people(h)
    if not credits:
        credits = d["directors"] + d["writers"] + d["cast_top"]

    episodes: list[dict] = []
    if not a.no_episodes and (d["is_series"] or has_eps):
        for h in _fetch_paged(ctx, f"{BASE}/work/{work_id}/episodes", use_cache):
            episodes += parse_episodes(h)
        episodes = list({e["episode_no"]: e for e in episodes}.values())

    if a.english:
        st, eh = ctx.f.get(f"{BASE}/en/work/{work_id}/", use_cache=use_cache)
        if st == 200 and eh:
            h1 = BeautifulSoup(eh, "lxml").find("h1")
            if h1:
                d["extra"]["title_en_page"] = clean(h1.get_text(" "))

    changed = ctx.db.save_work(d, credits, episodes)
    if a.extras:
        ex = collect_extras(ctx, work_id, html, flags, use_cache)
        ctx.db.save_extras(work_id, ex)
        if a.download_images:
            download_images(ctx, work_id, d.get("poster_url"), ex.get("gallery", []))
    ctx.db.mark(work_id, "done")
    d["_changed"] = changed
    return d


def collect_extras(ctx: Ctx, work_id: int, main_html: str, flags: dict, use_cache: bool) -> dict:
    """المواعيد/الدليل/الصور/التعليقات. نبدأ بما في الصفحة الرئيسية ثم نكمله من الصفحات الكاملة إن وُجدت روابطها."""
    sel = set(ctx.args.extras)
    out: dict[str, list] = {}
    if "showtimes" in sel:
        rows = parse_showtimes(main_html)
        if flags.get("_has_theater"):
            full: list[dict] = []
            for h in _fetch_paged(ctx, f"{BASE}/work/{work_id}/theater", False):  # لقطة لحظية: بلا كاش
                full += parse_showtimes(h)
            rows = full or rows
        out["showtimes"] = _dedup(rows, lambda r: (r["date"], r["theater_id"]))
    if "tvguide" in sel:
        rows = parse_tvguide(main_html)
        if flags.get("_has_tvguide"):
            full = []
            for h in _fetch_paged(ctx, f"{BASE}/work/{work_id}/tvguide", False):
                full += parse_tvguide(h)
            rows = full or rows
        out["tvguide"] = _dedup(rows, lambda r: (r["channel_id"], r["date_text"], r["time_raw"]))
    if "gallery" in sel:
        rows = parse_gallery(main_html, work_id)
        if flags.get("_has_gallery"):
            full = []
            for h in _fetch_paged(ctx, f"{BASE}/work/{work_id}/gallery", use_cache):
                full += parse_gallery(h, work_id)
            rows = full + rows
        out["gallery"] = _dedup(rows, lambda r: r["image_id"])
    if "comments" in sel:
        rows = parse_comments(main_html, "comment")
        for url in flags.get("_review_links", []):
            for h in _fetch_paged(ctx, url, use_cache):
                rows += parse_reviews(h)
        if ctx.args.comments_url:
            st, h = ctx.f.get(ctx.args.comments_url.format(id=work_id), use_cache=False)
            if st == 200 and h:
                rows += parse_comments(h, "comment")
        out["comments"] = _dedup(rows, lambda r: (r["kind"], r["comment_id"]))
    return out


def download_images(ctx: Ctx, work_id: int, poster_url: Optional[str], gallery: list[dict]) -> None:
    root = ctx.data_dir / "images" / str(work_id)

    def ext_of(u: str) -> str:
        m = re.search(r"\.(jpe?g|png|webp|gif)(?:$|\?)", u, re.I)
        return "." + m.group(1).lower() if m else ".jpg"

    if poster_url:
        ctx.f.download(poster_url, root / f"poster{ext_of(poster_url)}")
    for g in gallery[: ctx.args.max_images]:
        for u in (g.get("full_url_guess"), g.get("thumb_url")):
            if not u:
                continue
            dest = root / f"{g['image_id']}{ext_of(u)}"
            if ctx.f.download(u, dest):
                ctx.db.c.execute("UPDATE gallery SET local_path=? WHERE work_id=? AND image_id=?",
                                 (str(dest.relative_to(ctx.data_dir)), work_id, g["image_id"]))
                break
    ctx.db.c.commit()


def discover_sitemap(ctx: Ctx) -> list[tuple[int, Optional[str]]]:
    """يقرأ خرائط الموقع (من robots.txt أو /sitemap.xml) ويعيد [(work_id, lastmod)]."""
    start = list(ctx.f.sitemaps) or [BASE + "/sitemap.xml", BASE + "/sitemap_index.xml"]
    todo, seen_files, found = [(u, 0) for u in start], set(), {}
    while todo and len(seen_files) < 300:
        u, depth = todo.pop(0)
        if u in seen_files:
            continue
        seen_files.add(u)
        data = ctx.f.get_bytes(u)
        if not data:
            continue
        try:
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            soup = BeautifulSoup(data, "xml")
        except Exception as e:  # noqa: BLE001
            log.warning("تعذّر تحليل %s: %s", u, e)
            continue
        for sm in soup.find_all("sitemap"):
            loc = sm.find("loc")
            if loc and depth < 3:
                todo.append((loc.get_text().strip(), depth + 1))
        for node in soup.find_all("url"):
            loc = node.find("loc")
            m = WORK_RE.match(loc.get_text().strip()) if loc else None
            if m:
                lm = node.find("lastmod")
                found[int(m.group(1))] = lm.get_text().strip() if lm else None
    log.info("sitemap: فُحص %d ملفًا، أعمال: %d", len(seen_files), len(found))
    return sorted(found.items())


def probe_ahead(ctx: Ctx, max_misses: int, limit: int) -> list[dict]:
    """يجرّب معرّفات أكبر من أكبر عمل مخزّن (الأعمال الجديدة تأخذ معرّفات أعلى) حتى يتوالى max_misses إخفاقًا."""
    top = ctx.db.c.execute("SELECT MAX(work_id) FROM works").fetchone()[0]
    if not top:
        log.info("لا أعمال في القاعدة بعد؛ شغّل backfill أولًا ثم استخدم التتبع بالمعرّفات")
        return []
    found, misses, tried, i = [], 0, 0, top + 1
    while misses < max_misses and tried < limit:
        d = process_work(ctx, i, refresh=True)   # refresh: لا نثق بنسخة كاش لمعرّف كان فارغًا
        tried += 1
        if d:
            found.append(d)
            misses = 0
            log.info("  معرّف %d عمل جديد: %s", i, d["title_ar"])
        elif getattr(ctx, "last_state", None) == "skipped":
            misses = 0          # المعرّف موجود لكنه خارج نطاق السنوات؛ لا يُعدّ إخفاقًا
        else:
            misses += 1
        i += 1
    log.info("تتبع المعرّفات: جُرّب %d (من %d)، أعمال وُجدت %d", tried, top + 1, len(found))
    return found


def cmd_sitemap(ctx: Ctx):
    items = sitemap_filter(discover_sitemap(ctx), year_bounds(ctx.args)[0])
    if not items:
        print("لم أجد خريطة موقع تحوي روابط أعمال. اعتمد على: update --mode full و/أو --probe-misses.")
        return
    known = {r[0] for r in ctx.db.c.execute("SELECT work_id FROM works UNION SELECT work_id FROM queue")}
    new = [(w, lm) for w, lm in items if w not in known]
    print(f"أعمال في sitemap: {len(items)} | غير موجودة عندك: {len(new)}")
    top = sorted((lm for _, lm in items if lm), reverse=True)[:3]
    if top:
        print("أحدث lastmod:", ", ".join(top), "(يعني أن الخريطة تحمل تواريخ تعديل)")
    if not ctx.args.dry_run:
        n = sum(1 for w, _ in new if ctx.db.enqueue(w, "sitemap"))
        ctx.db.c.commit()
        print(f"أُضيف إلى الطابور: {n} (نفّذ fetch-pending لجلب تفاصيلها)")


def cmd_verify(ctx: Ctx) -> int:
    """تدقيق: هل ما على الموقع الآن موجود عندنا؟ وهل المهام المجدولة تعمل؟ يعيد 0 إن كل شيء سليم، 2 إن وُجدت مشاكل."""
    a, c = ctx.args, ctx.db.c
    problems: list[str] = []
    now = datetime.now(timezone.utc)
    mn, mx = year_bounds(a)

    print("═══ 1) صحة التشغيل التلقائي ═══")
    for cmd, limit_h, label in (("update", 3, "الفحص السريع (كل ساعة)"),
                                ("backfill", 30, "المسح الكامل (يوميًا)"),
                                ("schedules", 9, "المواعيد (كل 4 ساعات)")):
        row = c.execute("SELECT finished_at FROM runs WHERE cmd=? AND ok=1 ORDER BY id DESC LIMIT 1", (cmd,)).fetchone()
        if not row:
            print(f"  ✗ {label}: لم يكتمل أي تشغيل ناجح بعد")
            problems.append(f"{label}: لا تشغيل ناجح مسجّل (هل ثُبّتت المهام؟ راجع autorun.log)")
            continue
        age_h = (now - datetime.fromisoformat(row[0])).total_seconds() / 3600
        good = age_h <= limit_h
        print(f"  {'✓' if good else '✗'} {label}: آخر نجاح قبل {age_h:.1f} ساعة (الحد المقبول {limit_h})")
        if not good:
            problems.append(f"{label}: آخر نجاح قبل {age_h:.1f} ساعة")

    in_works = {r[0] for r in c.execute("SELECT work_id FROM works")}
    qstate = dict(c.execute("SELECT work_id, state FROM queue"))

    def classify(wid: int) -> str:
        if wid in in_works or qstate.get(wid) == "skipped":
            return "ok"
        if qstate.get(wid) == "missing":
            return "suspicious"          # الموقع يعرضه لكننا وسمناه «غير موجود»
        return "queued" if wid in qstate else "undiscovered"

    found: dict[str, dict[int, str]] = {"undiscovered": {}, "queued": {}, "suspicious": {}}

    def check(wid: int, title: Optional[str]) -> None:
        k = classify(wid)
        if k != "ok":
            found[k][wid] = title or ""

    print("\n═══ 2) مقارنة فهارس السنوات الحيّة بقاعدتك ═══")
    years = [y for y in discover_years(ctx.f) if y >= mn and (not mx or y <= mx)]
    total = 0
    for y in years:
        n = 0
        for r in crawl_listing(ctx.f, f"{BASE}/index/work/release_year/{y}/", a.max_pages):
            n += 1
            check(r["work_id"], r.get("title"))
        total += n
        print(f"  سنة {y}: {n} عمل في الفهرس")
    print(f"  المجموع على الموقع: {total}")

    print("\n═══ 3) مقارنة صفحات «الجديد» (يعرض الآن/قريبًا/القنوات) ═══")
    extra_found: dict[int, str] = {}
    for p in LATEST_SOURCES:
        st, html = ctx.f.get(BASE + p, use_cache=False)
        if st == 200 and html:
            for wid in extract_work_ids(html):
                if classify(wid) != "ok" and wid not in extra_found:
                    extra_found[wid] = p
                    check(wid, None)
    print(f"  أعمال ظاهرة هناك وغير معروفة لدينا: {len(extra_found)}")

    print("\n═══ 4) خريطة الموقع (sitemap) ═══")
    sm = sitemap_filter(discover_sitemap(ctx), mn)
    if sm:
        miss = [w for w, _ in sm if classify(w) != "ok"]
        print(f"  موجودة: {len(sm)} عمل، منها غير معروف لدينا: {len(miss)}")
        for w in miss[:200]:
            check(w, None)
    else:
        print("  غير متوفرة (لا بأس: الطبقات الأخرى تغطي)")

    print("\n═══ 5) تتبع المعرّفات (معرّفات أعلى من الأحدث عندك) ═══")
    top = max(in_works) if in_works else 0
    probed_new = 0
    if top and a.probe_check:
        for i in range(top + 1, top + 1 + a.probe_check):
            st, html = ctx.f.get(f"{BASE}/work/{i}/", use_cache=False)
            d = parse_work(html, i) if st == 200 and html else None
            if not d:
                ctx.f.invalidate(f"{BASE}/work/{i}/")
                continue
            if classify(i) != "ok" and ((not mn or (d["year"] or mn) >= mn) and (not mx or (d["year"] or 0) <= mx)):
                probed_new += 1
                check(i, d["title_ar"])
        print(f"  جُرّب {a.probe_check} معرّفًا بعد {top}؛ أعمال جديدة غير معروفة: {probed_new}")
    else:
        print("  تخطّيت (لا أعمال في القاعدة بعد أو --probe-check 0)")

    print("\n═══ 6) من أين اكتُشفت أعمالك (حسب المصدر) ═══")
    for src, n in c.execute("SELECT CASE WHEN source LIKE '%release_year%' OR source LIKE 'year:%' THEN 'فهارس السنوات' "
                            "WHEN source='sitemap' THEN 'sitemap' WHEN source='schedules' THEN 'صفحات الجديد/المواعيد' "
                            "WHEN source='direct' THEN 'تتبع المعرّفات' ELSE source END, COUNT(*) FROM queue "
                            "WHERE state IN ('done') GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {src}: {n}")

    print("\n═══ النتيجة ═══")
    und, que, sus = found["undiscovered"], found["queued"], found["suspicious"]
    print(f"  أعمال على الموقع ولم يكتشفها السكربت بعد : {len(und)}")
    print(f"  أعمال مكتشفة وبانتظار الجلب/إعادة المحاولة : {len(que)}")
    print(f"  أعمال على الموقع لكننا وسمناها «غير موجود»  : {len(sus)}")
    for label, d in (("غير مكتشف", und), ("غير موجود؟", sus)):
        for wid, t in list(d.items())[:10]:
            print(f"    - [{label}] {wid} {t}")
    if und:
        problems.append(f"{len(und)} عمل على الموقع لم يُكتشف (عادي إن أُضيف خلال الساعة الأخيرة؛ شغّل verify --fix)")
    if sus:
        problems.append(f"{len(sus)} عمل وُسم missing رغم ظهوره في الموقع (أرسل لي أمثلة منها)")

    if a.fix and (und or que):
        ids = list(und) + [w for w in que if qstate.get(w) in ("pending", "error")]
        for w in und:
            ctx.db.enqueue(w, "verify")
        ctx.db.c.commit()
        print(f"\n--fix: جلب {min(len(ids), a.max_new)} عملًا الآن...")
        fetch_ids(ctx, ids[: a.max_new])

    print()
    if problems:
        print("⚠ يوجد ما يحتاج انتباهًا:")
        for p in problems:
            print("  •", p)
        return 2
    print("✓ كل ما على الموقع (ضمن نطاق السنوات) موجود في قاعدتك، والتشغيل التلقائي حديث.")
    return 0


def cmd_schedules(ctx: Ctx):
    """يحدّث لقطة مواعيد السينمات ودليل القنوات للأعمال الظاهرة في صفحات الجديد/المعروض الآن."""
    ids, seen = [], set()
    for p in LATEST_SOURCES:
        st, html = ctx.f.get(BASE + p, use_cache=False)
        if st == 200 and html:
            for wid in extract_work_ids(html):
                if wid not in seen:
                    seen.add(wid)
                    ids.append(wid)
    if ctx.args.limit:
        ids = ids[: ctx.args.limit]
    log.info("أعمال لتحديث مواعيدها: %d", len(ids))
    sel = set(ctx.args.extras)
    known = {r[0] for r in ctx.db.c.execute("SELECT work_id FROM works")}
    skipped = {r[0] for r in ctx.db.c.execute("SELECT work_id FROM queue WHERE state='skipped'")}
    for i, wid in enumerate(ids, 1):
        if wid in skipped:
            continue                       # عمل قديم (مثل إعادات التلفزيون): لا نحفظ مواعيده
        ctx.db.enqueue(wid, "schedules")
        if wid not in known and year_bounds(ctx.args)[0]:
            if not process_work(ctx, wid):  # عمل جديد علينا: نتحقق من سنته أولًا ونحفظ تفاصيله إن كان ضمن النطاق
                continue
        res: dict[str, list] = {}
        for name, path, parser, keyf in (
            ("showtimes", "theater", parse_showtimes, lambda r: (r["date"], r["theater_id"])),
            ("tvguide", "tvguide", parse_tvguide, lambda r: (r["channel_id"], r["date_text"], r["time_raw"])),
        ):
            if name not in sel:
                continue
            htmls = _fetch_paged(ctx, f"{BASE}/work/{wid}/{path}", False)
            if htmls or ctx.last_status == 404:   # عند فشل مؤقت لا نمسح اللقطة القديمة
                rows: list[dict] = []
                for h in htmls:
                    rows += parser(h)
                res[name] = _dedup(rows, keyf)
        if res:
            ctx.db.save_extras(wid, res)
        if i % 25 == 0:
            log.info("[%d/%d]", i, len(ids))
    ctx.db.c.commit()


def fetch_ids(ctx: Ctx, ids: list[int], refresh: bool = False) -> list[dict]:
    done = []
    total = len(ids)
    for i, wid in enumerate(ids, 1):
        try:
            d = process_work(ctx, wid, refresh)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("فشل العمل %s", wid)
            ctx.db.mark(wid, "error", repr(e)[:300])
            continue
        if d:
            done.append(d)
            log.info("[%d/%d] %s  %s (%s) — %s", i, total, wid, d["title_ar"], d["year"], d["category_label"])
        elif i % 50 == 0:
            log.info("[%d/%d]", i, total)
    return done


# ----------------------------------------------------------------------------
# الأوامر
# ----------------------------------------------------------------------------
def cmd_inspect(ctx: Ctx):
    wid = ctx.args.work_id
    status, html = ctx.f.get(f"{BASE}/work/{wid}/", use_cache=not ctx.args.refresh)
    if status != 200 or not html:
        print(f"تعذّر الجلب (status={status})")
        return 1
    d = parse_work(html, wid)
    if not d:
        print("الصفحة ليست صفحة عمل أو تغيّرت بنيتها")
        return 1
    flags = {k: d.pop(k) for k in list(d) if k.startswith("_")}
    credits: list[dict] = []
    for h in _fetch_paged(ctx, f"{BASE}/work/{wid}/cast", not ctx.args.refresh):
        credits += parse_people(h)
    eps: list[dict] = []
    if d["is_series"]:
        for h in _fetch_paged(ctx, f"{BASE}/work/{wid}/episodes", not ctx.args.refresh):
            eps += parse_episodes(h)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    print(f"\ncredits={len(credits)}  episodes={len(eps)}")
    if ctx.args.extras:
        ex = collect_extras(ctx, wid, html, flags, not ctx.args.refresh)
        print("\nالإضافات:", {k: len(v) for k, v in ex.items()}, "| روابط الصفحة:",
              {k.lstrip("_"): v for k, v in flags.items() if k != "_has_episodes_page"})
        for k, v in ex.items():
            if v:
                print(f"  مثال {k}:", json.dumps(v[0], ensure_ascii=False))
        print("  (قد تكون المواعيد/الدليل فارغة لعمل غير معروض حاليًا، والتعليقات فارغة إن كانت تُحمَّل بـ JavaScript)")
    empty = [k for k in ("title_ar", "year", "category_label", "genres", "directors", "cast_top",
                         "synopsis", "release_dates", "countries") if not d.get(k)]
    if d["is_series"] and not eps:
        empty.append("episodes")
    if empty:
        print("⚠ حقول فارغة (قد تحتاج تعديل المحلّل أو أنها غير متوفرة للعمل):", ", ".join(empty))
    return 0


def cmd_discover(ctx: Ctx) -> list[int]:
    a, new_ids = ctx.args, []
    years = discover_years(ctx.f)
    if a.years:
        lo, _, hi = a.years.partition("-")
        lo, hi = int(lo), int(hi or lo)
        years = [y for y in years if lo <= y <= hi]
    mn, mx = year_bounds(a)
    years = [y for y in years if y >= mn and (not mx or y <= mx)]
    log.info("سنوات للفحص: %d  (النطاق: %s → %s)", len(years), mn or "بلا حد", mx or "بلا حد")
    sources = [f"{BASE}/index/work/release_year/{y}/" for y in years]
    if a.categories:
        if mn:
            log.warning("--categories يُتجاهل مع --min-year لأن فهارس الأنواع تضم كل السنوات القديمة.")
        else:
            sources += [f"{BASE}/index/work/category/{c}" for c in (1, 3)]
    for src in sources:
        n = 0
        for r in crawl_listing(ctx.f, src, a.max_pages):
            if ctx.db.enqueue(r["work_id"], src, r):
                new_ids.append(r["work_id"])
                n += 1
        ctx.db.c.commit()
        log.info("%s → جديد في الطابور: %d", src.replace(BASE, ""), n)
    return new_ids


def cmd_fetch_pending(ctx: Ctx):
    ids = ctx.db.pending_ids(ctx.args.limit)
    log.info("بانتظار الجلب: %d", len(ids))
    fetch_ids(ctx, ids)


def cmd_update(ctx: Ctx) -> list[dict]:
    a = ctx.args
    year = datetime.now().year
    srcs = [BASE + p for p in LATEST_SOURCES] + [f"{BASE}/ramadan/{year + 1}/", f"{BASE}/summary/{year}/"]
    srcs += list(a.extra_source or [])
    new_ids: list[int] = []
    for s in srcs:
        st, html = ctx.f.get(s, use_cache=False)
        if st != 200 or not html:
            continue
        for wid in extract_work_ids(html):
            if ctx.db.enqueue(wid, s):
                new_ids.append(wid)
    if a.mode == "full":
        for y in (year, year + 1):
            for r in crawl_listing(ctx.f, f"{BASE}/index/work/release_year/{y}/", a.max_pages):
                if ctx.db.enqueue(r["work_id"], f"year:{y}", r):
                    new_ids.append(r["work_id"])
    n_pages = len(new_ids)
    if getattr(a, "sitemap", False):
        for wid, _lm in sitemap_filter(discover_sitemap(ctx), year_bounds(a)[0]):
            if ctx.db.enqueue(wid, "sitemap"):
                new_ids.append(wid)
    n_sitemap = len(new_ids) - n_pages
    ctx.db.c.commit()
    log.info("أعمال جديدة مكتشفة: %d (صفحات/فهارس=%d، sitemap=%d)", len(new_ids), n_pages, n_sitemap)
    cap = getattr(a, "max_new", 200)
    if len(new_ids) > cap:
        log.warning("العدد أكبر من --max-new=%d: سأجلب %d الآن وتُكمَل البقية تلقائيًا في الدورات القادمة", cap, cap)
    done = fetch_ids(ctx, new_ids[:cap])
    first = set(new_ids[:cap])
    leftovers = [i for i in ctx.db.pending_ids(cap) if i not in first]   # عالق من أخطاء سابقة أو فائض سقف
    if leftovers:
        log.info("إكمال %d عملًا عالقًا في الطابور", len(leftovers))
        done += fetch_ids(ctx, leftovers)
    if getattr(a, "probe_misses", 0):
        probed = probe_ahead(ctx, a.probe_misses, a.probe_limit)
        done += probed
    if done:
        out = ctx.data_dir / "new_items.jsonl"
        with open(out, "a", encoding="utf-8") as fh:
            for d in done:
                rec = {k: d.get(k) for k in ("work_id", "url", "title_ar", "title_alt", "year",
                                              "category_label", "release_dates", "detected_at")}
                rec["detected_at"] = now_iso()
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"\n— أعمال جديدة ({len(done)}):")
        for d in done:
            print(f"  • [{d['category_label']}] {d['title_ar']} ({d['year']})  {d['url']}")
    else:
        print("لا أعمال جديدة.")
    return done


def cmd_watch(ctx: Ctx):
    log.info("بدء المراقبة كل %d ثانية (Ctrl+C للإيقاف)", ctx.args.interval)
    while True:
        try:
            cmd_update(ctx)
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001
            log.exception("فشلت دورة التحديث، ستُعاد في الدورة القادمة")
        time.sleep(ctx.args.interval)


def cmd_export(ctx: Ctx):
    a = ctx.args
    out = Path(a.out)
    db = ctx.db.c
    db.row_factory = sqlite3.Row
    works = [dict(r) for r in db.execute("SELECT * FROM works ORDER BY work_id")]
    if a.format == "csv" and a.table != "works":
        if a.table not in ("credits", "episodes", "showtimes", "tv_airings", "gallery", "comments"):
            raise SystemExit(f"جدول غير معروف: {a.table}")
        rows = [dict(r) for r in db.execute(f"SELECT * FROM {a.table}")]
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["work_id"])
            w.writeheader()
            w.writerows(rows)
        print(f"تم تصدير {len(rows)} صف من {a.table} إلى {out}")
        return
    if a.format == "csv":
        with open(out, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(works[0].keys()) if works else WORK_COLS)
            w.writeheader()
            w.writerows(works)
    else:
        credits: dict[int, list] = {}
        for r in db.execute("SELECT * FROM credits"):
            credits.setdefault(r["work_id"], []).append(
                {"person_id": r["person_id"], "name": r["person_name"], "section": r["section"], "role": r["role"]})
        eps: dict[int, list] = {}
        for r in db.execute("SELECT * FROM episodes ORDER BY episode_no"):
            eps.setdefault(r["work_id"], []).append({"episode_no": r["episode_no"], "summary": r["summary"]})
        extra_tbl: dict[str, dict[int, list]] = {}
        for t in ("showtimes", "tv_airings", "gallery", "comments"):
            for r in db.execute(f"SELECT * FROM {t}"):
                row = {k: r[k] for k in r.keys() if k != "work_id"}
                if t == "showtimes" and row.get("times"):
                    row["times"] = json.loads(row["times"])
                extra_tbl.setdefault(t, {}).setdefault(r["work_id"], []).append(row)
        with open(out, "w", encoding="utf-8") as fh:
            for w in works:
                for k in ("countries", "languages", "genres", "release_dates", "directors", "writers",
                          "cast_top", "filming_locations", "watch_links", "header_items", "extra"):
                    if w.get(k):
                        try:
                            w[k] = json.loads(w[k])
                        except (TypeError, ValueError):
                            pass
                w["credits"] = credits.get(w["work_id"], [])
                w["episodes"] = eps.get(w["work_id"], [])
                for t in ("showtimes", "tv_airings", "gallery", "comments"):
                    w[t] = extra_tbl.get(t, {}).get(w["work_id"], [])
                fh.write(json.dumps(w, ensure_ascii=False) + "\n")
    print(f"تم تصدير {len(works)} عمل إلى {out}")


def cmd_stats(ctx: Ctx):
    q = ctx.db.c.execute
    print("works          :", q("SELECT COUNT(*) FROM works").fetchone()[0])
    print("  أفلام        :", q("SELECT COUNT(*) FROM works WHERE category_id=1").fetchone()[0])
    print("  مسلسلات      :", q("SELECT COUNT(*) FROM works WHERE is_series=1").fetchone()[0])
    print("credits        :", q("SELECT COUNT(*) FROM credits").fetchone()[0])
    print("episodes       :", q("SELECT COUNT(*) FROM episodes").fetchone()[0])
    print("showtimes      :", q("SELECT COUNT(*) FROM showtimes").fetchone()[0])
    print("tv_airings     :", q("SELECT COUNT(*) FROM tv_airings").fetchone()[0])
    print("gallery        :", q("SELECT COUNT(*) FROM gallery").fetchone()[0])
    print("comments       :", q("SELECT COUNT(*) FROM comments").fetchone()[0])
    for st, n in q("SELECT state, COUNT(*) FROM queue GROUP BY state"):
        print(f"queue[{st}]".ljust(15), ":", n)


def build_parser() -> argparse.ArgumentParser:
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--data-dir", default="elcinema_data", help="مجلد البيانات (القاعدة + الكاش)")
    c.add_argument("--delay", type=float, default=1.5, help="ثوانٍ بين الطلبات (الافتراضي 1.5)")
    c.add_argument("--user-agent", default=DEFAULT_UA, help="هوية العميل (حروف إنجليزية فقط)؛ يُفضّل أن تتضمن وسيلة تواصل")
    c.add_argument("--ignore-robots", action="store_true", help="تجاهل robots.txt (فقط إن كان ترخيصك يغطي ذلك)")
    c.add_argument("--no-cast", action="store_true", help="لا تجلب صفحة الطاقم الكاملة")
    c.add_argument("--no-episodes", action="store_true", help="لا تجلب حلقات المسلسلات")
    c.add_argument("--english", action="store_true", help="اجلب صفحة /en/ أيضًا لاستخراج العنوان الإنجليزي")
    c.add_argument("--max-pages", type=int, default=1000, help="أقصى عدد صفحات لكل فهرس")
    c.add_argument("--min-year", type=int, default=2026,
                   help="لا يُحفَظ إلا الأعمال التي سنتها >= هذه القيمة (الافتراضي 2026؛ 0 = بلا حد أدنى)")
    c.add_argument("--max-year", type=int, default=0,
                   help="حد أعلى للسنة (0 = بلا حد، فتدخل الأعمال المعلنة للسنوات القادمة مثل رمضان 2027)")
    c.add_argument("--extras", default="all",
                   help="الإضافات: all | none | قائمة مفصولة بفواصل من showtimes,tvguide,gallery,comments")
    c.add_argument("--download-images", action="store_true", help="نزّل الملصق وصور المعرض إلى data-dir/images")
    c.add_argument("--max-images", type=int, default=50, help="أقصى عدد صور معرض لكل عمل عند التنزيل")
    c.add_argument("--comments-url", help="قالب رابط لتعليقات تُحمَّل بـ AJAX، مثل https://elcinema.com/xxx/{id}")
    c.add_argument("--log-file", help="اكتب السجل والمخرجات في هذا الملف بدل الشاشة (للتشغيل المجدول)")
    c.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(prog="elcinema_scraper", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inspect", parents=[c]); s.add_argument("work_id", type=int)
    s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("discover", parents=[c])
    s.add_argument("--years", help="نطاق سنوات مثل 2020-2026 أو 2026")
    s.add_argument("--categories", action="store_true", help="افحص أيضًا فهارس الأفلام/المسلسلات (لأعمال بلا سنة)")
    s = sub.add_parser("fetch-pending", parents=[c]); s.add_argument("--limit", type=int)
    s = sub.add_parser("backfill", parents=[c])
    s.add_argument("--years"); s.add_argument("--categories", action="store_true"); s.add_argument("--limit", type=int)
    s = sub.add_parser("update", parents=[c])
    s.add_argument("--mode", choices=["fast", "full"], default="fast",
                   help="fast: صفحات الجديد فقط. full: + فهارس السنة الحالية والقادمة")
    s.add_argument("--extra-source", action="append", help="رابط صفحة إضافية تُفحص بحثًا عن أعمال جديدة")
    s.add_argument("--sitemap", action="store_true", help="افحص خريطة الموقع أيضًا بحثًا عن أعمال جديدة")
    s.add_argument("--probe-misses", type=int, default=0,
                   help="تتبع المعرّفات: جرّب معرّفات أعلى من الأحدث عندك حتى يتوالى هذا العدد من الإخفاقات (0=معطّل)")
    s.add_argument("--probe-limit", type=int, default=2000, help="أقصى عدد معرّفات تُجرَّب في الدورة")
    s.add_argument("--max-new", type=int, default=200, help="أقصى عدد أعمال جديدة تُجلب تفاصيلها في الدورة")
    s = sub.add_parser("watch", parents=[c])
    s.add_argument("--interval", type=int, default=3600)
    s.add_argument("--mode", choices=["fast", "full"], default="fast")
    s.add_argument("--extra-source", action="append")
    s.add_argument("--sitemap", action="store_true")
    s.add_argument("--probe-misses", type=int, default=0)
    s.add_argument("--probe-limit", type=int, default=2000)
    s.add_argument("--max-new", type=int, default=200)
    s = sub.add_parser("fetch", parents=[c])
    s.add_argument("--ids", type=int, nargs="+", required=True); s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("export", parents=[c])
    s.add_argument("--format", choices=["csv", "jsonl"], default="jsonl"); s.add_argument("--out", default="elcinema_export.jsonl")
    s.add_argument("--table", default="works",
                   help="مع csv: works | credits | episodes | showtimes | tv_airings | gallery | comments")
    s = sub.add_parser("schedules", parents=[c]); s.add_argument("--limit", type=int)
    s = sub.add_parser("sitemap", parents=[c]); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("verify", parents=[c])
    s.add_argument("--fix", action="store_true", help="اجلب فورًا ما وُجد ناقصًا")
    s.add_argument("--probe-check", type=int, default=25, help="عدد المعرّفات التي تُجرَّب بعد الأحدث (0=تخطّي)")
    s.add_argument("--max-new", type=int, default=200, help="أقصى ما يُجلب مع --fix")
    sub.add_parser("stats", parents=[c])
    return p


def main(argv=None) -> int:
    # ويندوز: طرفية cp850/cp1252 تُسقط الطباعة العربية؛ نجعل المخرجات UTF-8 (مع استبدال ما لا يُعرض)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = build_parser().parse_args(argv)
    try:
        args.user_agent.encode("latin-1")
    except UnicodeEncodeError:
        print("تنبيه: --user-agent يحتوي أحرفًا غير لاتينية (غير مسموحة في ترويسات HTTP)، استُبدل بالافتراضي. "
              "استخدم حروفًا إنجليزية، مثل: \"my-project (contact: you@example.com)\"", file=sys.stderr)
        args.user_agent = DEFAULT_UA
    ex = args.extras
    args.extras = EXTRAS_ALL if ex == "all" else (() if ex == "none" else tuple(x.strip() for x in ex.split(",") if x.strip()))
    bad = [x for x in args.extras if x not in EXTRAS_ALL]
    if bad:
        raise SystemExit(f"--extras غير صالح: {bad}")
    if getattr(args, "log_file", None):
        lp = Path(args.log_file)
        lp.parent.mkdir(parents=True, exist_ok=True)
        if lp.exists() and lp.stat().st_size > 5_000_000:      # تدوير بسيط: نحتفظ بنسخة قديمة واحدة
            lp.replace(lp.with_suffix(lp.suffix + ".old"))
        sys.stdout = sys.stderr = open(lp, "a", encoding="utf-8", buffering=1)   # يعمل أيضًا تحت pythonw (بلا نافذة)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.user_agent == DEFAULT_UA:
        log.info("نصيحة: مرّر --user-agent \"my-project (contact: you@example.com)\" (حروف إنجليزية فقط) ليتمكن الموقع من التواصل معك.")
    lock = ctx = None
    started, ok = now_iso(), False
    locked = args.cmd in LOCKED_CMDS or (args.cmd == "verify" and args.fix)
    if locked:
        data = Path(args.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        lock = RunLock(data / "run.lock")
        if not lock.acquire():
            log.info("تشغيل آخر قيد التنفيذ (run.lock)؛ تخطّيت هذه الدورة.")
            return 0
    try:
        ctx = Ctx(args)
        if lock:
            ctx.f.heartbeat = lock.touch
        if args.cmd == "inspect":
            return cmd_inspect(ctx)
        if args.cmd == "discover":
            cmd_discover(ctx)
        elif args.cmd == "fetch-pending":
            cmd_fetch_pending(ctx)
        elif args.cmd == "backfill":
            cmd_discover(ctx)
            cmd_fetch_pending(ctx)
        elif args.cmd == "update":
            cmd_update(ctx)
        elif args.cmd == "watch":
            cmd_watch(ctx)
        elif args.cmd == "fetch":
            for wid in args.ids:
                ctx.db.enqueue(wid, "manual")
            ctx.db.c.commit()
            fetch_ids(ctx, args.ids, args.refresh)
        elif args.cmd == "export":
            cmd_export(ctx)
        elif args.cmd == "schedules":
            cmd_schedules(ctx)
        elif args.cmd == "sitemap":
            cmd_sitemap(ctx)
        elif args.cmd == "verify":
            rc = cmd_verify(ctx)
            ok = True
            return rc
        elif args.cmd == "stats":
            cmd_stats(ctx)
        ok = True
    except KeyboardInterrupt:
        log.info("تم الإيقاف. التقدّم محفوظ — أعد تشغيل نفس الأمر للاستئناف.")
        return 130
    finally:
        try:
            if ctx is not None and args.cmd in LOCKED_CMDS:
                ctx.db.log_run(args.cmd, started, ok)       # يغذّي فحص «صحة التشغيل التلقائي» في verify
        except Exception:  # noqa: BLE001
            pass
        if lock:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
