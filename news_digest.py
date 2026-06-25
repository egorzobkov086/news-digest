#!/usr/bin/env python3
"""
Утренняя сводка новостей по заданной тематике.
Запуск:  python news_digest.py [тема]
Вывод:  news_digest.html
"""

import sys, re, math, hashlib, urllib.parse, datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass

try:
    import requests
except ImportError:
    sys.exit("Установите requests:  pip install requests")

try:
    import feedparser
    FEEDPARSER_OK = True
except ImportError:
    FEEDPARSER_OK = False
    print("feedparser не найден - использую xml.etree. Рекомендуется: pip install feedparser")

try:
    import pymorphy2
    MORPH = pymorphy2.MorphAnalyzer()
    PYMORPHY_OK = True
except ImportError:
    PYMORPHY_OK = False
    MORPH = None
    print("pymorphy2 не найден - используйте pip install pymorphy2 для точного поиска словоформ")

# ======================== КОНФИГУРАЦИЯ ========================

TOPIC      = "искусственный интеллект"
LANG       = "ru"
COUNTRY    = "RU"
MAX_NEWS   = 25
OUT_FILE   = "news_digest.html"

W_SOURCE   = 0.30
W_RELEVANT = 0.35
W_RECENCY  = 0.20
W_QUALITY  = 0.15

SOURCE_AUTHORITY = {
    # Российские информагентства
    "ria": 9.0, "тасс": 9.5, "tass": 9.5,
    "interfax": 9.0, "рбк": 8.5, "rbc": 8.5,
    "коммерсантъ": 8.5, "kommersant": 8.5,
    "ведомости": 8.5, "vedomosti": 8.5,
    "lenta.ru": 7.0, "lenta": 7.0,
    "газета.ru": 7.0, "iz.ru": 7.5, "известия": 7.5,
    "rg.ru": 7.5, "российская газета": 7.5,
    # IT / технологии
    "cnews": 7.5, "habr": 7.5, "3dnews": 7.0,
    "ixbt": 7.0, "vc.ru": 7.0, "vc": 7.0,
    # Международные
    "forbes": 8.0, "bbc": 8.0, "reuters": 9.0,
    "bloomberg": 8.5, "cnn": 7.5,
    "the verge": 7.0, "techcrunch": 7.5,
    "meduza": 6.0, "the bell": 7.0,
}
DEFAULT_AUTHORITY = 5.0

# Основные российские RSS-ленты (проверены, работают).
# Каждый элемент: (название_источника, url)
RSS_FEEDS = [
    # IT / технологии — высокая вероятность новостей по AI/ИИ
    ("CNews",          "https://www.cnews.ru/inc/rss/news.xml"),
    ("Habr",           "https://habr.com/ru/rss/all/all/?fl=ru"),
    ("3DNews",         "https://3dnews.ru/news/rss"),
    ("iXBT",           "https://www.ixbt.com/export/rss.xml"),
    # Общие новостные
    ("Коммерсантъ",    "https://www.kommersant.ru/RSS/news.xml"),
    ("ТАСС",           "https://tass.ru/rss/v2.xml"),
    ("Интерфакс",      "https://www.interfax.ru/rss.asp"),
    ("Ведомости",      "https://www.vedomosti.ru/rss/news"),
    ("Лента.ру",       "https://lenta.ru/rss/"),
    ("РИА Новости",    "https://ria.ru/export/rss2/index.xml"),
    ("Российская газета", "https://rg.ru/xml/index.xml"),
    ("VC.ru",          "https://vc.ru/rss"),
]

# Резервные RSS-ленты (могут не работать)
RSS_FEEDS_EXTRA = [
    ("РБК",            "https://rssexport.rbc.ru/rbcnews/news/20/full.rss"),
]

# Google News (часто недоступен из РФ; включается опционально)
RSS_GOOGLE_NEWS = f"https://news.google.com/rss/search?q={{q}}&hl={LANG}&gl={COUNTRY}&ceid={COUNTRY}:{LANG}"
TRY_GOOGLE = False  # Google News заблокирован в РФ, не тратим время
GOOGLE_TIMEOUT = 8   # секунд на попытку

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# ======================== МОДЕЛЬ ========================

@dataclass
class Article:
    title: str
    link: str
    source: str
    published: dt.datetime | None = None
    snippet: str = ""
    relevance: float = 0.0
    authority: float = 0.0
    recency: float = 0.0
    quality: float = 0.0
    score: float = 0.0

# ======================== СБОР НОВОСТЕЙ ========================

def _clean_html(html_text: str) -> str:
    if not html_text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", html_text)
    clean = re.sub(r"&[a-z]+;", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:2000]

def _parse_date(date_str: str) -> dt.datetime | None:
    if not date_str:
        return None
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%a, %d %b %Y %H:%M:%S %Z",
    ]:
        try:
            return dt.datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None

def _extract_source_from_entry(entry, title: str, fallback_source: str = "Unknown") -> str:
    """Извлечь название источника из RSS-записи (feedparser) или заголовка."""
    src = entry.get("source", {})
    if isinstance(src, dict):
        name = src.get("title", "")
        if name:
            return name
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        src_name = parts[1].strip() if len(parts) > 1 else ""
        if src_name:
            return src_name
    return fallback_source

def _parse_rss_feedparser(xml_text: str, feed_source_name: str) -> list:
    """Парсинг RSS через feedparser."""
    feed = feedparser.parse(xml_text)
    articles = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link  = entry.get("link", "").strip()
        if not title or not link:
            continue
        source = _extract_source_from_entry(entry, title, feed_source_name)
        pub = _parse_date(entry.get("published", "") or entry.get("updated", ""))
        # Пробуем взять полный текст из разных полей RSS/Atom
        raw_content = (
            entry.get("summary", "")
            or entry.get("description", "")
        )
        # Atom: content[0].value — часто полный текст статьи
        content_list = entry.get("content", [])
        if content_list and isinstance(content_list, list):
            content_val = content_list[0].get("value", "") if isinstance(content_list[0], dict) else ""
            if len(content_val) > len(raw_content):
                raw_content = content_val
        # RSS 2.0: content:encoded
        encoded = entry.get("content:encoded", "")
        if len(encoded) > len(raw_content):
            raw_content = encoded
        snippet = _clean_html(raw_content)
        articles.append(Article(title=title, link=link, source=source,
                                published=pub, snippet=snippet))
    return articles

def _parse_rss_etree(xml_text: str, feed_source_name: str) -> list:
    """Запасной парсер RSS через xml.etree.ElementTree."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    articles = []
    for item in root.findall(".//item"):
        title_e  = item.find("title")
        link_e   = item.find("link")
        pub_e    = item.find("pubDate")
        desc_e   = item.find("description")
        source_e = item.find("source")

        title = title_e.text.strip() if title_e is not None and title_e.text else ""
        link  = link_e.text.strip()  if link_e  is not None and link_e.text else ""
        if not title or not link:
            continue

        source = source_e.text.strip() if source_e is not None and source_e.text else ""
        if not source:
            if " - " in title:
                src_part = title.rsplit(" - ", 1)
                source = src_part[1].strip() if len(src_part) > 1 else ""
        if not source:
            source = feed_source_name

        pub = _parse_date(pub_e.text) if pub_e is not None else None
        desc_text = desc_e.text if desc_e is not None else ""
        # Попробовать content:encoded (RSS 2.0 — полный текст статьи)
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        enc_e = item.find("content:encoded", ns)
        enc_text = enc_e.text if enc_e is not None and enc_e.text else ""
        if len(enc_text) > len(desc_text):
            desc_text = enc_text
        snippet = _clean_html(desc_text)
        articles.append(Article(title=title, link=link, source=source,
                                published=pub, snippet=snippet))
    return articles

def _is_relevant(title: str, snippet: str, topic: str) -> bool:
    """Проверить, относится ли статья к заданной теме."""
    text = (title + " " + snippet).lower()
    text_raw = title + " " + snippet

    topic_lower = topic.lower().strip()

    # 1. Прямое совпадение полной фразы (например, "искусственный интеллект")
    if topic_lower in text:
        return True

    # 2. Синонимы и аббревиатуры (проверяются с учётом контекста)
    if "искусственный интеллект" in topic_lower:
        # "ИИ" как аббревиатура — только отдельным словом (не часть другого слова)
        if re.search(r"(?:^|\s|[,.!?;:()\-\"«])ИИ(?:$|\s|[,.!?;:()\-\"»])", text_raw):
            return True
        # "AI" как английская аббревиатура — отдельным словом
        if re.search(r"(?:^|\s|[,.!?;:()\-\"«])AI(?:$|\s|[,.!?;:()\-\"»])", text_raw):
            return True
        # Словоформы (родительный падеж и т.д.)
        if "искусственного интеллекта" in text:
            return True

    elif "машинное обучение" in topic_lower:
        if re.search(r"\bML\b", text_raw):
            return True
        if "machine learning" in text:
            return True

    # 3. Для многословных тем: все ключевые слова должны присутствовать
    topic_words = [w.lower() for w in topic.split() if len(w) >= 3]
    if len(topic_words) >= 2:
        if all(w in text for w in topic_words):
            return True

    # 4. Для однословных тем: pymorphy2 или запасной стемминг
    if len(topic_words) == 1:
        w = topic_words[0]
        if w in text:
            return True

        if PYMORPHY_OK:
            try:
                parsed = MORPH.parse(w)[0]
                normal = parsed.normal_form
                if normal != w and normal in text:
                    return True
                for form in parsed.lexeme:
                    fw = form.word.lower()
                    if fw != w and fw in text:
                        return True
            except Exception:
                pass
        else:
            # Запасной вариант без pymorphy2: только точное совпадение + простые окончания
            for suffix in ("а", "у", "ом", "е", "ы", "ов", "ам", "ами", "ах", "ой", "ый", "ий", "ого", "ому"):
                if (w + suffix) in text:
                    return True
        return False

    return False

def _normalize_title(title: str) -> str:
    """Нормализовать заголовок для дедупликации."""
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t[:80]

def fetch_all_news(topic: str) -> list:
    """Собрать новости из всех RSS-источников, отфильтровать по теме."""
    all_articles: list[Article] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()

    # --- основные российские источники ---
    for source_name, url in RSS_FEEDS:
        print(f"  [{source_name}] запрос...")
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"       ошибка: {e}")
            continue

        xml_text = resp.text
        if FEEDPARSER_OK:
            try:
                parsed = _parse_rss_feedparser(xml_text, source_name)
            except Exception as e:
                print(f"       feedparser: {e}, fallback etree")
                parsed = _parse_rss_etree(xml_text, source_name)
        else:
            parsed = _parse_rss_etree(xml_text, source_name)

        # фильтрация по теме
        relevant = [a for a in parsed if _is_relevant(a.title, a.snippet, topic)]
        print(f"       всего {len(parsed)}, релевантных {len(relevant)}")

        for art in relevant:
            norm = _normalize_title(art.title)
            if art.link not in seen_links and norm not in seen_titles:
                seen_links.add(art.link)
                seen_titles.add(norm)
                all_articles.append(art)

    # --- дополнительные источники ---
    for source_name, url in RSS_FEEDS_EXTRA:
        print(f"  [{source_name}] запрос...")
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"       ошибка: {e}")
            continue

        xml_text = resp.text
        if FEEDPARSER_OK:
            try:
                parsed = _parse_rss_feedparser(xml_text, source_name)
            except Exception:
                parsed = _parse_rss_etree(xml_text, source_name)
        else:
            parsed = _parse_rss_etree(xml_text, source_name)

        relevant = [a for a in parsed if _is_relevant(a.title, a.snippet, topic)]
        print(f"       всего {len(parsed)}, релевантных {len(relevant)}")
        for art in relevant:
            norm = _normalize_title(art.title)
            if art.link not in seen_links and norm not in seen_titles:
                seen_links.add(art.link)
                seen_titles.add(norm)
                all_articles.append(art)

    # --- Google News (опционально, может не работать) ---
    if TRY_GOOGLE:
        google_url = RSS_GOOGLE_NEWS.format(q=urllib.parse.quote(topic))
        print(f"  [Google News] запрос (timeout={GOOGLE_TIMEOUT}с)...")
        try:
            resp = requests.get(google_url, headers=HTTP_HEADERS, timeout=GOOGLE_TIMEOUT)
            resp.raise_for_status()
            if FEEDPARSER_OK:
                try:
                    parsed = _parse_rss_feedparser(resp.text, "Google News")
                except Exception:
                    parsed = _parse_rss_etree(resp.text, "Google News")
            else:
                parsed = _parse_rss_etree(resp.text, "Google News")
            print(f"       получено {len(parsed)} новостей")
            for art in parsed:
                norm = _normalize_title(art.title)
                if art.link not in seen_links and norm not in seen_titles:
                    seen_links.add(art.link)
                    seen_titles.add(norm)
                    all_articles.append(art)
        except Exception as e:
            print(f"       ошибка: {e} (Google News недоступен)")

    print(f"\n  Итого уникальных новостей: {len(all_articles)}")
    return all_articles

# ======================== РАНЖИРОВАНИЕ ========================

def _source_authority(source: str) -> float:
    src_lower = source.lower().strip()
    for key, val in SOURCE_AUTHORITY.items():
        if key in src_lower:
            return val
    return DEFAULT_AUTHORITY

def _title_relevance(title: str, topic: str) -> float:
    title_lower = title.lower()
    topic_words = [w.lower() for w in topic.split() if len(w) > 2]
    if not topic_words:
        return 5.0
    matched = 0.0
    for w in topic_words:
        if w in title_lower:
            if w in title_lower[:40]:
                matched += 1.2
            else:
                matched += 1.0
    ratio = min(matched / len(topic_words), 2.0)
    return ratio * 5.0

def _recency_score(pub_date: dt.datetime | None) -> float:
    if pub_date is None:
        return 5.0
    now = dt.datetime.now(pub_date.tzinfo) if pub_date.tzinfo else dt.datetime.now()
    age_hours = max((now - pub_date).total_seconds() / 3600, 0)
    return 10.0 / (1.0 + max(age_hours / 12, 0))

def _title_quality(title: str) -> float:
    if not title:
        return 0.0
    score = 5.0
    length = len(title)
    if 30 <= length <= 120:
        score += 2.0
    elif length < 15:
        score -= 3.0
    elif length > 200:
        score -= 1.0
    clickbait_patterns = [
        r"(?i)\b(шок|сенсаци|неожидан|вы не поверите|всего за|срочно)\b",
        r"[!?]{2,}",
        r"(?i)\b(ТОП|TOP)\s*\d",
    ]
    for pat in clickbait_patterns:
        if re.search(pat, title):
            score -= 2.0
            break
    upper_ratio = sum(1 for c in title if c.isupper()) / max(len(title), 1)
    if upper_ratio > 0.5:
        score -= 2.0
    return max(0.0, min(10.0, score))

def rank_articles(articles: list, topic: str) -> list:
    for art in articles:
        art.authority = _source_authority(art.source)
        art.relevance = _title_relevance(art.title, topic)
        art.recency   = _recency_score(art.published)
        art.quality   = _title_quality(art.title)
        art.score = round(
            W_SOURCE   * art.authority +
            W_RELEVANT * art.relevance +
            W_RECENCY  * art.recency +
            W_QUALITY  * art.quality, 1
        )
    articles.sort(key=lambda a: a.score, reverse=True)
    return articles

# ======================== ГЕНЕРАЦИЯ HTML ========================

def _source_color(source: str) -> str:
    h = hashlib.md5(source.strip().lower().encode()).hexdigest()
    hue = int(h[:6], 16) % 360
    return f"hsl({hue}, 55%, 48%)"

def _score_bar(score: float) -> str:
    pct = min(int(score * 10), 100)
    if score >= 7.5:
        clr = "#2ecc71"
    elif score >= 5.0:
        clr = "#f39c12"
    else:
        clr = "#95a5a6"
    return (
        f'<span class="score-bar" style="background:linear-gradient(to right,{clr} {pct}%,#eee {pct}%)">'
        f'{score}</span>'
    )

CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f5f6fa;color:#2c3e50;padding:20px}
.container{max-width:1100px;margin:0 auto}
.header{background:#fff;border-radius:10px;padding:28px 32px;margin-bottom:20px;
        box-shadow:0 1px 4px rgba(0,0,0,.06)}
.header h1{font-size:1.7rem;font-weight:700;margin-bottom:6px}
.header .topic{color:#2980b9}
.header .meta{font-size:.85rem;color:#7f8c8d;margin-top:8px}
.stats{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.stat-card{background:#fff;border-radius:10px;padding:16px 20px;flex:1 1 180px;
           box-shadow:0 1px 4px rgba(0,0,0,.06);text-align:center}
.stat-card .num{font-size:2rem;font-weight:700;color:#2980b9}
.stat-card .lbl{font-size:.8rem;color:#7f8c8d;margin-top:4px}
.table-wrap{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.06);overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:700px}
th{background:#f8f9fa;font-size:.78rem;text-transform:uppercase;
   letter-spacing:.04em;color:#7f8c8d;padding:12px 16px;text-align:left;
   border-bottom:2px solid #e0e4e8}
td{padding:10px 16px;border-bottom:1px solid #e0e4e8;font-size:.92rem;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr.news-row{cursor:pointer;transition:background .15s}
tr.news-row:hover{background:#f0f4ff}
tr.news-row.active{background:#e8f0fe}
.rank{width:40px;font-weight:700;color:#7f8c8d;text-align:center}
.title-cell a{color:#2c3e50;text-decoration:none}
.title-cell a:hover{color:#2980b9;text-decoration:underline}
.source-badge{display:inline-block;padding:3px 10px;border-radius:12px;
              font-size:.78rem;font-weight:600;white-space:nowrap;
              border-left:4px solid var(--src-clr,#999);padding-left:8px;
              background:#f0f0f5}
.score-cell{width:90px;text-align:center}
.score-bar{display:inline-block;font-weight:700;font-size:.85rem;
           padding:2px 8px;border-radius:6px;min-width:42px;color:#fff;
           text-shadow:0 1px 2px rgba(0,0,0,.2)}
.date-cell{width:90px;color:#7f8c8d;font-size:.85rem;white-space:nowrap}
.sources-footer{margin-top:20px;padding:16px 20px;background:#fff;
                border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.06);
                font-size:.82rem;color:#7f8c8d}
.sources-footer .chip{display:inline-block;padding:2px 8px;margin:2px 4px;
                       border-radius:10px;font-size:.76rem;background:#eee;color:#444}
/* Панель чтения новости */
.overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.35);
         z-index:99;display:none}
.overlay.show{display:block}
.reader-panel{position:fixed;top:0;right:0;width:480px;max-width:92vw;height:100vh;
              background:#fff;z-index:100;box-shadow:-4px 0 20px rgba(0,0,0,.15);
              transform:translateX(100%);transition:transform .3s ease;
              display:flex;flex-direction:column;overflow-y:auto}
.reader-panel.open{transform:translateX(0)}
.reader-header{display:flex;align-items:flex-start;justify-content:space-between;
               padding:20px 24px;border-bottom:1px solid #e0e4e8;gap:12px}
.reader-header h2{font-size:1.15rem;line-height:1.4;color:#2c3e50;flex:1}
.reader-close{background:none;border:none;font-size:1.6rem;cursor:pointer;
              color:#7f8c8d;padding:0 4px;line-height:1;flex-shrink:0}
.reader-close:hover{color:#e74c3c}
.reader-meta{padding:14px 24px;border-bottom:1px solid #f0f2f5;font-size:.82rem;color:#7f8c8d}
.reader-meta-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:6px}
.reader-meta-row:last-child{margin-bottom:0}
.reader-importance{display:flex;align-items:center;gap:6px;font-size:.8rem}
.reader-importance .imp-dot{width:10px;height:10px;border-radius:50%;display:inline-block;flex-shrink:0}
.reader-importance .imp-label{font-weight:600}
.reader-body{padding:20px 24px;flex:1;overflow-y:auto;font-size:.95rem;line-height:1.75;
             color:#2c3e50}
.reader-body p{margin-bottom:14px;text-indent:1.5em}
.reader-body p:first-child{font-weight:500;font-size:1.02rem;color:#1a252f}
.reader-body .no-content{color:#95a5a6;font-style:italic;text-indent:0;text-align:center;padding:30px 0}
.reader-footer{padding:16px 24px;border-top:1px solid #e0e4e8;display:flex;gap:10px;
               flex-wrap:wrap}
.reader-footer a{display:inline-block;padding:8px 18px;border-radius:6px;
                 text-decoration:none;font-size:.88rem;font-weight:600}
.btn-original{background:#2980b9;color:#fff}
.btn-original:hover{background:#2471a3}
.btn-close-panel{background:#ecf0f1;color:#2c3e50}
.btn-close-panel:hover{background:#dde4e6}
@media(max-width:600px){
  body{padding:10px}
  .header{padding:16px}
  .header h1{font-size:1.3rem}
  .stats{flex-direction:column}
  .stat-card{padding:12px}
  .reader-panel{width:100vw;max-width:100vw}
}
"""

def generate_html(articles: list, topic: str) -> str:
    """Собрать HTML-страницу сводки с панелью чтения."""
    import json as _json

    now = dt.datetime.now().strftime("%d.%m.%Y, %H:%M")
    sources_list = sorted({a.source for a in articles})

    articles_json = []
    rows = ""
    for rank, art in enumerate(articles, 1):
        pub_str = art.published.strftime("%d.%m %H:%M") if art.published else "-"
        color = _source_color(art.source)
        snippet_esc = art.snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        sid = f"n{rank}"
        articles_json.append({
            "id": sid,
            "title": art.title,
            "link": art.link,
            "source": art.source,
            "date": pub_str,
            "score": art.score,
            "snippet": art.snippet,
            "color": color,
        })
        rows += (
            f'<tr class="news-row" data-id="{sid}">'
            f'<td class="rank">{rank}</td>'
            f'<td class="title-cell">{art.title}</td>'
            f'<td><span class="source-badge" style="--src-clr:{color}">{art.source}</span></td>'
            f'<td class="date-cell">{pub_str}</td>'
            f'<td class="score-cell">{_score_bar(art.score)}</td>'
            f'</tr>\n'
        )

    max_score = max((a.score for a in articles), default=0)
    high_importance = sum(1 for a in articles if a.score >= 7.0)
    source_chips = "".join(
        f'<span class="chip" style="border-left:4px solid {_source_color(s)}">{s}</span> '
        for s in sources_list
    )
    art_data_json = _json.dumps(articles_json, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Сводка новостей - {topic}</title>
<style>{CSS}</style>
</head>
<body>
<div class="overlay" id="overlay"></div>
<div class="reader-panel" id="readerPanel">
  <div class="reader-header">
    <h2 id="readerTitle"></h2>
    <button class="reader-close" id="readerClose">&times;</button>
  </div>
  <div class="reader-meta" id="readerMeta"></div>
  <div class="reader-body" id="readerBody"></div>
  <div class="reader-footer">
    <a class="btn-original" id="readerLink" href="#" target="_blank" rel="noopener">Читать оригинал &rarr;</a>
    <a class="btn-close-panel" href="#" id="readerCloseBtn">Закрыть</a>
  </div>
</div>

<div class="container">
<div class="header">
<h1>Сводка новостей по теме <span class="topic">{topic}</span></h1>
<div class="meta">
Сгенерировано: {now} &middot;
Источников: {len(sources_list)} &middot;
Язык: {LANG} / {COUNTRY}
<br><small>Кликните на строку, чтобы прочитать новость здесь</small>
</div>
</div>
<div class="stats">
<div class="stat-card"><div class="num">{len(articles)}</div><div class="lbl">новостей</div></div>
<div class="stat-card"><div class="num">{max_score:.1f}</div><div class="lbl">макс. важность</div></div>
<div class="stat-card"><div class="num">{high_importance}</div><div class="lbl">важных (>=7.0)</div></div>
<div class="stat-card"><div class="num">{len({a.source for a in articles})}</div><div class="lbl">источников</div></div>
</div>
<div class="table-wrap">
<table>
<thead><tr><th>#</th><th>Заголовок</th><th>Источник</th><th>Дата</th><th>Важность</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<div class="sources-footer">
<strong>Источники в сводке:</strong><br>
{source_chips}
</div>
</div>

<script>
(function() {{
    var articles = {art_data_json};
    var panel = document.getElementById('readerPanel');
    var overlay = document.getElementById('overlay');
    var titleEl = document.getElementById('readerTitle');
    var metaEl = document.getElementById('readerMeta');
    var bodyEl = document.getElementById('readerBody');
    var linkEl = document.getElementById('readerLink');
    var activeRow = null;

    function esc(s) {{
        return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}

    function importanceColor(score) {{
        if (score >= 7.5) return '#2ecc71';
        if (score >= 5.0) return '#f39c12';
        return '#95a5a6';
    }}

    function importanceLabel(score) {{
        if (score >= 7.5) return 'Высокая';
        if (score >= 5.0) return 'Средняя';
        return 'Низкая';
    }}

    function splitParagraphs(text) {{
        if (!text || text.trim().length === 0) return ['<p class="no-content">(текст аннотации отсутствует)</p>'];
        var sentences = text.split(/(?<=[.!?])\s+/);
        if (sentences.length <= 2) return ['<p>' + esc(text) + '</p>'];
        var parts = [];
        var buf = '';
        for (var i = 0; i < sentences.length; i++) {{
            var s = sentences[i].trim();
            if (!s) continue;
            buf += (buf ? ' ' : '') + s;
            if (buf.length > 120 || i === sentences.length - 1) {{
                parts.push('<p>' + esc(buf) + '</p>');
                buf = '';
            }}
        }}
        if (buf.trim()) parts.push('<p>' + esc(buf) + '</p>');
        return parts;
    }}

    function openArticle(art) {{
        titleEl.textContent = art.title;
        linkEl.href = art.link;

        var impColor = importanceColor(art.score);
        var impLabel = importanceLabel(art.score);

        metaEl.innerHTML =
            '<div class="reader-meta-row">' +
                '<span class="source-badge" style="--src-clr:' + art.color + '">' + art.source + '</span>' +
                '<span>' + art.date + '</span>' +
            '</div>' +
            '<div class="reader-meta-row">' +
                '<div class="reader-importance">' +
                    '<span class="imp-dot" style="background:' + impColor + '"></span>' +
                    '<span class="imp-label">' + impLabel + ' важность</span>' +
                    '<span>(' + art.score.toFixed(1) + ' из 10)</span>' +
                '</div>' +
            '</div>';

        bodyEl.innerHTML = splitParagraphs(art.snippet).join('');
        panel.classList.add('open');
        overlay.classList.add('show');
    }}

    function closePanel() {{
        panel.classList.remove('open');
        overlay.classList.remove('show');
        if (activeRow) {{
            activeRow.classList.remove('active');
            activeRow = null;
        }}
    }}

    document.querySelectorAll('.news-row').forEach(function(row) {{
        row.addEventListener('click', function(e) {{
            if (activeRow) activeRow.classList.remove('active');
            activeRow = row;
            row.classList.add('active');
            var id = row.getAttribute('data-id');
            var art = articles.find(function(a) {{ return a.id === id; }});
            if (art) openArticle(art);
        }});
    }});

    document.getElementById('readerClose').addEventListener('click', closePanel);
    document.getElementById('readerCloseBtn').addEventListener('click', function(e) {{
        e.preventDefault();
        closePanel();
    }});
    overlay.addEventListener('click', closePanel);

    document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape') closePanel();
    }});
}})();
</script>
</body>
</html>"""

# ======================== MAIN ========================

def main():
    # Если тема передана аргументом — тихий режим, иначе — интерактивный
    if len(sys.argv) > 1:
        topic = sys.argv[1]
        interactive = False
    else:
        interactive = True
        import os as _os; _os.system("cls" if _os.name == "nt" else "clear")
        print("=" * 50)
        print("=== УТРЕННЯЯ СВОДКА НОВОСТЕЙ ===")
        print("=" * 50)
        print(f"\nТема по умолчанию: {TOPIC}")
        topic = input("Введите тему (или Enter — использовать тему по умолчанию): ").strip()
        if not topic:
            topic = TOPIC
        print()

    print(f"Тема: {topic}")
    print()

    articles = fetch_all_news(topic)
    if not articles:
        print("Новостей не найдено.")
        if interactive:
            input("\nНажмите Enter для выхода...")
        return

    articles = rank_articles(articles, topic)
    articles = articles[:MAX_NEWS]

    html = generate_html(articles, topic)

    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    topic_slug = re.sub(r"[^\w]+", "_", topic.lower().strip())[:40]
    # Сохраняем рядом со скриптом, чтобы при двойном клике файл был там же
    script_dir = Path(__file__).resolve().parent
    out_path = script_dir / f"news_{topic_slug}_{date_str}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nСводка сохранена: {out_path}")
    print(f"Новостей в сводке: {len(articles)}")

    if interactive:
        print()
        input("Нажмите Enter для выхода...")

if __name__ == "__main__":
    main()