#!/usr/bin/env python3
"""
Crawl good news sources and write data/articles.json.
Runs server-side (no CORS issues). Called by GitHub Actions every 6 hours.
"""
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

# ── Sources ───────────────────────────────────────────────────────────────────
RSS_SOURCES = [
    {'id': 'nasa',     'label': 'NASA',                  'category': 'space',
     'url': 'https://www.nasa.gov/news-release/feed'},
    {'id': 'positive', 'label': 'Positive News',          'category': 'general',
     'url': 'https://www.positive.news/feed/'},
    {'id': 'rtbc',     'label': 'Reasons to be Cheerful', 'category': 'general',
     'url': 'https://reasonstobecheerful.world/feed/'},
    {'id': 'tpn',      'label': 'The Progress Network',   'category': 'general',
     'url': 'https://theprogressnetwork.org/feed/'},
    {'id': 'fcrunch',  'label': 'Future Crunch',           'category': 'general',
     'url': 'https://futurecrunch.beehiiv.com/feed'},
    {'id': 'gnn',      'label': 'Good News Network',       'category': 'general',
     'url': 'https://www.goodnewsnetwork.org/feed/'},
    {'id': 'ggg',      'label': 'Good Good Good',          'category': 'general',
     'url': 'https://www.goodgoodgood.co/articles/rss.xml'},
]

REDDIT_SOURCES = [
    {'id': 'r-uplifting',  'label': 'r/UpliftingNews',   'category': 'human',   'sub': 'UpliftingNews'},
    {'id': 'r-hbb',        'label': 'r/HumansBeingBros', 'category': 'human',   'sub': 'HumansBeingBros'},
    {'id': 'r-space',      'label': 'r/space',            'category': 'space',   'sub': 'space'},
    {'id': 'r-science',    'label': 'r/science',          'category': 'science', 'sub': 'science'},
    {'id': 'r-futurology', 'label': 'r/Futurology',       'category': 'general', 'sub': 'Futurology'},
]

NEGATIVE_TERMS = [
    # Security / crime
    'breach', 'breached', 'hack', 'hacked', 'vulnerab',
    'fraud', 'scam', 'leaked', 'data breach', 'ransomware', 'phishing',
    'arrest', 'arrested', 'lawsuit', 'indicted', 'convicted',
    'murder', 'murdered', 'murders', 'homicide',
    'shooting', 'shot dead', 'gun violence',
    'trafficking', 'smuggling', 'kidnap',
    # Conflict / politics
    ' war ', ' wars ', 'warfare', 'combat', 'airstrike', 'missile strike',
    'attack', 'attacks', 'attacked', 'bombing', 'explosion', 'exploded',
    'coup', 'insurgent', 'terrorist', 'terrorism',
    'killing', 'killed', 'death toll', 'casualties',
    # Corporate / economic bad news
    'layoff', 'layoffs', 'laid off', 'job cut', 'mass firing',
    'banned', 'ban ', 'shutdown', 'collapse', 'collapsed', 'bankrupt',
    'recession', 'crash ', 'market crash',
    # Alarming framing words (the ones that signal doom-and-gloom journalism)
    'alarming', 'alarmingly',
    'catastroph',          # catastrophe, catastrophic, catastrophically
    'chilling',
    'concerning',
    'deadly',
    'devastat',            # devastating, devastation, devastate
    ' dire', 'dire ',   # "dire warning", "situation is dire" — avoids 'directed' etc.
    'disaster', 'disastrous',
    'destructi',           # destruction, destructive, destructively
    'deteriorat',          # deteriorating, deterioration
    'epidemic',
    'horrific', 'horrifying',
    'outbreak',
    'plague',
    'scary',
    'suffer', 'suffering',
    'terrifying', 'terrified',
    'threaten', 'threatens', 'threatened', 'threatening',
    'tragic', 'tragedy',
    'troubling',
    'violent', 'violence',
    'wildfire',
    'worrying', 'worrisome',
    # Corruption / scandal
    'corrupt', 'corruption', 'scandal', 'misconduct', 'bribery',
    # Crisis / emergency framing
    'crisis', 'crises', 'emergency', 'famine', 'starvation',
]

# ── Utilities ─────────────────────────────────────────────────────────────────
def strip_html(text):
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&apos;', "'", text)
    text = re.sub(r'&#?\w+;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def truncate(text, max_len=220):
    if not text or len(text) <= max_len:
        return text or ''
    cut = text.rfind(' ', 0, max_len)
    return text[:cut if cut > 0 else max_len] + '\u2026'

def parse_date_ms(date_str):
    if not date_str:
        return int(time.time() * 1000)
    # RFC 2822 (RSS pubDate)
    try:
        return int(parsedate_to_datetime(date_str).timestamp() * 1000)
    except Exception:
        pass
    # ISO 8601 (Atom published/updated)
    try:
        s = date_str.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp() * 1000)
    except Exception:
        pass
    return int(time.time() * 1000)

def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; GoodNewsBot/1.0; +https://github.com/vvanhee/good-news)',
        'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        print(f'  WARN: {url} — {e}')
        return None

def title_passes_negative_filter(title):
    """Returns False if the title contains any negative/alarming framing term."""
    lo = title.lower()
    return not any(neg in lo for neg in NEGATIVE_TERMS)

# ── RSS parser ────────────────────────────────────────────────────────────────
def strip_namespaces(xml_text):
    """Remove XML namespace declarations and prefixes for simpler parsing."""
    xml_text = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_text)
    xml_text = re.sub(r'<(\w+):', '<', xml_text)
    xml_text = re.sub(r'</(\w+):', '</', xml_text)
    return xml_text

def parse_rss(source, xml_text):
    articles = []
    try:
        clean = strip_namespaces(xml_text)
        root  = ET.fromstring(clean)
    except ET.ParseError as e:
        print(f'  WARN: XML parse error for {source["id"]}: {e}')
        return articles

    # RSS 2.0: items inside channel; Atom: entry elements at root level
    items   = root.findall('.//item')
    is_atom = False
    if not items:
        items   = root.findall('.//entry')
        is_atom = True

    for item in items:
        def get(tag):
            el = item.find('.//' + tag)
            return (el.text or '').strip() if el is not None else ''

        title = get('title')
        if not title:
            continue

        if is_atom:
            link_el = item.find('.//link')
            url = (link_el.get('href') or '').strip() if link_el is not None else ''
            if not url:
                url = get('link')
            date_str = get('published') or get('updated')
        else:
            url      = get('link') or get('guid')
            date_str = get('pubDate') or get('date')

        url = url.strip()
        if not url.startswith('http'):
            continue

        desc = get('description') or get('summary') or get('encoded') or get('content')

        articles.append({
            'url':      url,
            'title':    title,
            'excerpt':  truncate(strip_html(desc)),
            'source':   source['label'],
            'sourceId': source['id'],
            'category': source['category'],
            'dateMs':   parse_date_ms(date_str),
        })

    return articles

# ── Fetchers ──────────────────────────────────────────────────────────────────
def crawl_rss():
    all_articles = []
    for src in RSS_SOURCES:
        print(f'RSS  {src["label"]}')
        xml = fetch_url(src['url'])
        if xml:
            arts = parse_rss(src, xml)
            if src.get('filter'):
                before = len(arts)
                arts = [a for a in arts if title_passes_negative_filter(a['title'])]
                print(f'     {len(arts)} articles ({before - len(arts)} filtered)')
            else:
                print(f'     {len(arts)} articles')
            all_articles.extend(arts)
    return all_articles

def crawl_reddit():
    all_articles = []
    for src in REDDIT_SOURCES:
        print(f'RDDT {src["label"]}')
        url  = f'https://www.reddit.com/r/{src["sub"]}.json?limit=25&raw_json=1'
        text = fetch_url(url)
        if not text:
            continue
        try:
            data  = json.loads(text)
            posts = data.get('data', {}).get('children', [])
            count = 0
            for post in posts:
                p = post.get('data', {})
                if p.get('stickied') or p.get('score', 0) < 50:
                    continue
                link    = p.get('url') or f'https://www.reddit.com{p.get("permalink", "")}'
                excerpt = p.get('selftext', '').replace('\n', ' ')
                all_articles.append({
                    'url':      link,
                    'title':    p.get('title', ''),
                    'excerpt':  truncate(excerpt),
                    'source':   src['label'],
                    'sourceId': src['id'],
                    'category': src['category'],
                    'dateMs':   int(p.get('created_utc', time.time()) * 1000),
                })
                count += 1
            print(f'     {count} articles')
        except Exception as e:
            print(f'     ERROR: {e}')
    return all_articles

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs('data', exist_ok=True)

    all_articles = []
    all_articles.extend(crawl_rss())
    all_articles.extend(crawl_reddit())

    # Deduplicate by URL
    seen, deduped = set(), []
    for a in all_articles:
        if a['url'] and a['url'] not in seen:
            seen.add(a['url'])
            deduped.append(a)

    deduped.sort(key=lambda x: x['dateMs'], reverse=True)

    output = {
        'crawledAt': int(time.time() * 1000),
        'articles':  deduped,
    }

    with open('data/articles.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

    print(f'\nSaved {len(deduped)} articles to data/articles.json')

if __name__ == '__main__':
    main()
