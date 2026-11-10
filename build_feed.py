#!/usr/bin/env python3
"""build_feed.py [new_items.json | --sync-only | --brief CAT FILE | --briefs-dir DIR]
合并新条目或每日简报、同步收藏 Issue、应用 30 天老化规则、生成 RSS/首页/归档页/简报页，然后 git push。

简报入库（按新闻条目拆开，写入对应分类，不把整份简报当成一条）：
  python3 build_feed.py --brief ai /path/to/2026-11-10.md
  python3 build_feed.py --briefs-dir /path/to/briefs   # 其下 ai|auto|robot/YYYY-MM-DD.md
CAT 只能是 ai、auto、robot。同一天同一分类的旧条目会被覆盖。
"""
import sys, json, os, html, subprocess, datetime, email.utils, re, urllib.request, urllib.error, shutil, pathlib, hashlib

R = "/home/box/x-hot-feed"
if os.environ.get("GITHUB_WORKSPACE"):
    R = os.environ["GITHUB_WORKSPACE"]
D = f"{R}/data/items.json"
REPO = "DukeYang/x-hot-feed"
BASE = "https://dukeyang.github.io/x-hot-feed"
CAT = {"auto": "汽车", "ai": "AI", "robot": "机器人"}
OWNER = "DukeYang"
AGE_DAYS = 30
RSS_LIMIT = 100


BRIEF_CAT = {"ai": "AI", "auto": "汽车", "robot": "机器人"}
BRIEF_TITLE = {"ai": "AI 每日简报", "auto": "汽车每日简报", "robot": "机器人每日简报"}
# 同一天三类条目的时间在 8:00 前后约 8 分钟内随机错开（按日期稳定，重复生成不变）
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))

def is_brief(i):
    return i.get("kind") in ("brief", "brief-item")

def item_link(i):
    if i.get("url"):
        return i["url"]
    if is_brief(i):
        key = i.get("brief_key") or {v: k for k, v in BRIEF_CAT.items()}.get(i.get("category"), "ai")
        date = i.get("brief_date") or ""
        iid = i.get("id") or ""
        m = re.search(r"-(\d+)$", iid)
        anchor = f"#item-{m.group(1)}" if m else ""
        return f"{BASE}/briefs/{key}/{date}.html{anchor}"
    return f"https://x.com/{i['user']}/status/{i['id']}"

def inline_md(s):
    codes, links = [], []
    def code_ph(m):
        codes.append(m.group(1))
        return f"\x00C{len(codes)-1}\x00"
    s = re.sub(r"`([^`]+)`", code_ph, s)
    def link_ph(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00L{len(links)-1}\x00"
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", link_ph, s)
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    def bare(m):
        u = m.group(0).rstrip(").,，。；;")
        return f'<a href="{u}" target="_blank" rel="noopener">{u}</a>'
    s = re.sub(r"https?://[^\s<]+", bare, s)
    for i, (label, url) in enumerate(links):
        s = s.replace(f"\x00L{i}\x00", f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(label)}</a>')
    for i, c in enumerate(codes):
        s = s.replace(f"\x00C{i}\x00", f"<code>{html.escape(c)}</code>")
    return s

def md_to_html(md):
    lines = (md or "").replace("\r\n", "\n").strip().split("\n")
    out, buf, items, mode = [], [], [], None
    def flush():
        nonlocal mode, buf, items
        if mode == "ul" and items:
            out.append("<ul>" + "".join(f"<li>{inline_md(x)}</li>" for x in items) + "</ul>")
        elif mode == "ol" and items:
            out.append("<ol>" + "".join(f"<li>{inline_md(x)}</li>" for x in items) + "</ol>")
        elif buf:
            if len(buf) == 1 and re.match(r"^\*\*.+\*\*\s*$", buf[0].strip()):
                out.append(f"<h2>{inline_md(buf[0].strip())}</h2>")
            else:
                out.append("<p>" + "<br>\n".join(inline_md(x) for x in buf) + "</p>")
        mode, buf, items = None, [], []
    for ln in lines:
        if not ln.strip():
            flush()
            continue
        um = re.match(r"^[-*]\s+(.*)$", ln)
        om = re.match(r"^(\d+)\.\s+(.*)$", ln)
        if um:
            if mode != "ul":
                flush()
                mode = "ul"
            items.append(um.group(1))
        elif om:
            if mode != "ol":
                flush()
                mode = "ol"
            items.append(om.group(2))
        else:
            if mode in ("ul", "ol"):
                flush()
            mode = "p"
            buf.append(ln)
    flush()
    return "\n".join(out)

ITEM_HEAD = re.compile(r"^\*\*(\d+)\.\s*(.+?)\*\*\s*$")
STOP_HEAD = re.compile(r"^\*\*(X 上的动态|推荐选题)\*\*\s*$")

def parse_brief_news(md):
    """Split a daily brief into numbered news items. Skip 今日主线, X 动态 and 推荐选题."""
    found = []
    cur = None
    skipping = False
    for ln in (md or "").replace("\r\n", "\n").split("\n"):
        s = ln.strip()
        if STOP_HEAD.match(s):
            if cur:
                found.append(cur)
                cur = None
            skipping = True
            continue
        m = ITEM_HEAD.match(s)
        if m:
            skipping = False
            if cur:
                found.append(cur)
            cur = {"title": m.group(2).strip(), "lines": []}
            continue
        if cur and not skipping:
            cur["lines"].append(ln)
    if cur:
        found.append(cur)
    for it in found:
        it["text"] = "\n".join(it["lines"]).strip()
        del it["lines"]
    return found

def item_summary(title, body):
    for para in re.split(r"\n\s*\n", body or ""):
        p = para.strip()
        if not p or p.startswith("来源") or p.startswith("选题角度"):
            continue
        if p.startswith("此前"):
            continue
        plain = re.sub(r"\s+", " ", re.sub(r"\*\*", "", p)).strip()
        plain = re.sub(r"^新增[：:]\s*", "", plain)
        if plain:
            return plain[:180] + ("…" if len(plain) > 180 else "")
    m = re.search(r"选题角度[：:]\s*(.+)", body or "")
    if m:
        return m.group(1).strip()[:180]
    return title

def first_source_url(body, fallback):
    block = body or ""
    m = re.search(r"来源[：:][^\n]+", block)
    if m:
        block = m.group(0)
    m = re.search(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", block)
    if m:
        return m.group(2)
    m = re.search(r"https?://[^\s)\]>]+", block)
    return m.group(0) if m else fallback

def brief_item_time(cat_key, date_s, seq):
    y, mo, d = [int(x) for x in date_s.split("-")]
    base = datetime.datetime(y, mo, d, 8, 0, 0, tzinfo=SHANGHAI)
    n = int(hashlib.sha256(f"{date_s}:{cat_key}:{seq}".encode()).hexdigest()[:8], 16)
    offset = (n % (16 * 60 + 1)) - 8 * 60  # -8 到 +8 分钟
    dt = base + datetime.timedelta(seconds=offset)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def make_brief_items(cat_key, path, date_s=None):
    cat_key = cat_key.lower()
    if cat_key not in BRIEF_CAT:
        raise ValueError(f"unknown brief category: {cat_key}")
    path = str(path)
    if not date_s:
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", pathlib.Path(path).name)
        if not m:
            raise ValueError(f"cannot parse date from {path}")
        date_s = m.group(1)
    text = open(path, encoding="utf-8").read().strip()
    if not text:
        raise ValueError(f"empty brief: {path}")
    news = parse_brief_news(text)
    out = []
    for seq, it in enumerate(news, 1):
        sid = f"{seq:02d}"
        fallback = f"{BASE}/briefs/{cat_key}/{date_s}.html#item-{sid}"
        out.append({
            "id": f"brief-{cat_key}-{date_s}-{sid}",
            "kind": "brief-item",
            "user": "每日简报",
            "name": "每日简报",
            "category": BRIEF_CAT[cat_key],
            "title": it["title"],
            "summary": item_summary(it["title"], it["text"]),
            "text": it["text"],
            "created_at": brief_item_time(cat_key, date_s, seq),
            "metrics": {},
            "media": [],
            "url": first_source_url(it["text"], fallback),
            "brief_date": date_s,
            "brief_key": cat_key,
        })
    return out

def collect_briefs_from_dir(root):
    root = pathlib.Path(root)
    items = []
    for key in ("ai", "auto", "robot"):
        d = root / key
        if not d.is_dir():
            continue
        for fp in sorted(d.glob("20??-??-??.md")):
            items.extend(make_brief_items(key, fp, fp.stem))
    return items

def drop_replaced_briefs(items, incoming):
    keys = {(i.get("brief_key"), i.get("brief_date")) for i in incoming if i.get("brief_key") and i.get("brief_date")}
    def keep(i):
        iid = i.get("id") or ""
        if re.fullmatch(r"brief-(ai|auto|robot)-\d{4}-\d{2}-\d{2}", iid):
            return False
        if is_brief(i) and (i.get("brief_key"), i.get("brief_date")) in keys:
            return False
        return True
    return [i for i in items if keep(i)]

def sort_items(items):
    items.sort(key=lambda i: i.get("created_at") or i.get("added_at") or "", reverse=True)
    return items

def page_shell(title, body, extra_nav=""):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="../../assets/style.css">
</head>
<body>
<header><div class="bar">
<h1>{html.escape(title)}</h1>
<div class="navlinks">
<a href="../../index.html">首页</a>
{extra_nav}
</div>
</div></header>
{body}
<footer>内容来自每日简报整理，供选题与素材使用</footer>
</body></html>
"""

def write_brief_pages(items):
    groups = {}
    for i in items:
        if not is_brief(i):
            continue
        key, date = i.get("brief_key"), i.get("brief_date")
        if key and date:
            groups.setdefault((key, date), []).append(i)
    os.makedirs(f"{R}/briefs", exist_ok=True)
    by_date = {}
    for (key, date), its in groups.items():
        its = sorted(its, key=lambda x: x.get("created_at") or "")
        os.makedirs(f"{R}/briefs/{key}", exist_ok=True)
        parts = [f'<div class="article-wrap"><p class="lead">{html.escape(BRIEF_CAT.get(key, key))} · {html.escape(date)}</p>']
        for i in its:
            m = re.search(r"-(\d+)$", i.get("id") or "")
            sid = m.group(1) if m else ""
            parts.append(f'<h2 id="item-{sid}">{html.escape(i.get("title") or "")}</h2>')
            parts.append(f'<article class="article">{md_to_html(i.get("text") or "")}</article>')
        parts.append("</div>")
        title = f"{BRIEF_TITLE.get(key, '简报')} · {date}"
        open(f"{R}/briefs/{key}/{date}.html", "w").write(page_shell(title, "\n".join(parts)))
        by_date.setdefault(date, []).extend(its)
    parts = []
    for date in sorted(by_date.keys(), reverse=True):
        parts.append(f"<h2>{html.escape(date)}</h2><ul>")
        for i in sorted(by_date[date], key=lambda x: x.get("created_at") or ""):
            rel = f"{i['brief_key']}/{i['brief_date']}.html"
            m = re.search(r"-(\d+)$", i.get("id") or "")
            if m:
                rel += f"#item-{m.group(1)}"
            parts.append(f'<li><a href="{rel}"><strong>{html.escape(i.get("title") or "")}</strong><small>{html.escape(i.get("summary") or "")}</small></a></li>')
        parts.append("</ul>")
    if not parts:
        parts.append('<p style="color:var(--sub);padding:40px 0;text-align:center">暂无条目</p>')
    open(f"{R}/briefs/index.html", "w").write(f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>简报条目</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<header><div class="bar">
<h1>按日期查看</h1>
<div class="navlinks">
<a href="../index.html">首页</a>
<a href="../archive/index.html">收藏归档</a>
</div>
</div></header>
<div class="brief-index">
{''.join(parts)}
</div>
<footer>内容来自每日简报整理，供选题与素材使用</footer>
</body></html>
""")

os.makedirs(f"{R}/data", exist_ok=True)
os.makedirs(f"{R}/archive/img", exist_ok=True)
os.makedirs(f"{R}/assets", exist_ok=True)

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def run(cmd, check=False, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=check, **kw)

def gh_json(args):
    p = run(["gh"] + args)
    if p.returncode != 0:
        return None, p.stderr.strip()
    try:
        return json.loads(p.stdout or "null"), ""
    except Exception as e:
        return None, str(e)

def ensure_fav_label():
    p = run(["gh", "label", "list", "-R", REPO, "--json", "name"])
    names = set()
    if p.returncode == 0:
        try:
            names = {x["name"] for x in json.loads(p.stdout)}
        except Exception:
            pass
    if "fav" not in names:
        run(["gh", "label", "create", "fav", "-R", REPO,
             "--description", "收藏/取消收藏同步", "--color", "F2C94C"])

def close_issue(number, body):
    run(["gh", "issue", "close", str(number), "-R", REPO, "--comment", body])

def sync_favorites(items):
    """List open fav issues by DukeYang; apply fav/unfav; close issues. Returns (applied, errors)."""
    ensure_fav_label()
    data, err = gh_json(["issue", "list", "-R", REPO, "--label", "fav",
                         "--state", "open", "--limit", "100",
                         "--json", "number,title,author,labels"])
    # Also pick up issues without label yet (title starts with 收藏/取消收藏) — Actions may race label
    if data is None:
        data = []
    # Extra: open issues by title pattern if Actions fires before label attach
    extra, _ = gh_json(["issue", "list", "-R", REPO, "--state", "open", "--limit", "50",
                        "--json", "number,title,author,labels",
                        "--search", "author:DukeYang in:title 收藏"])
    by_num = {i["number"]: i for i in (data or [])}
    for i in (extra or []):
        by_num.setdefault(i["number"], i)
    issues = list(by_num.values())

    by_id = {i["id"]: i for i in items}
    applied = []
    for iss in issues:
        author = (iss.get("author") or {}).get("login") or ""
        if author != OWNER:
            close_issue(iss["number"], f"忽略：仅接受 @{OWNER} 的收藏 Issue（作者 @{author}）。")
            continue
        title = (iss.get("title") or "").strip()
        m = re.match(r"^(取消收藏|收藏)\s+(\d+)\s*$", title)
        if not m:
            # also accept titles that merely start with the verbs
            m2 = re.match(r"^(取消收藏|收藏)\s+(\d+)", title)
            if not m2:
                close_issue(iss["number"], f"无法解析标题「{title}」，期望「收藏 <id>」或「取消收藏 <id>」。")
                continue
            m = m2
        action, iid = m.group(1), m.group(2)
        item = by_id.get(iid)
        if not item:
            close_issue(iss["number"], f"未知帖子 id={iid}，已关闭。")
            continue
        if action == "收藏":
            item["fav"] = True
            item["fav_at"] = item.get("fav_at") or now_utc().isoformat()
            # Download images immediately on favorite
            download_item_images(item)
            close_issue(iss["number"], f"已收藏 {iid}，图片已归档备份。下次/本次构建已同步。")
            applied.append(("fav", iid))
        else:
            item["fav"] = False
            item.pop("fav_at", None)
            close_issue(iss["number"], f"已取消收藏 {iid}。")
            applied.append(("unfav", iid))
    return applied, err

def ext_from_url(url):
    path = url.split("?")[0]
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return ext.lstrip(".")
    return "jpg"

def download_item_images(item):
    """Download remote twitter images into archive/img/<id>_<n>.<ext>; rewrite media to local paths."""
    media = item.get("media") or []
    if not media:
        return
    local = []
    for n, url in enumerate(media[:8]):
        if not url:
            continue
        if isinstance(url, str) and not url.startswith("http"):
            # already local
            local.append(url)
            continue
        ext = ext_from_url(url)
        rel = f"archive/img/{item['id']}_{n}.{ext}"
        dest = f"{R}/{rel}"
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            local.append(rel)
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "x-hot-feed/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
            if not data:
                continue
            if "png" in ctype:
                rel = f"archive/img/{item['id']}_{n}.png"
            elif "webp" in ctype:
                rel = f"archive/img/{item['id']}_{n}.webp"
            elif "gif" in ctype:
                rel = f"archive/img/{item['id']}_{n}.gif"
            else:
                rel = f"archive/img/{item['id']}_{n}.{ext}"
            dest = f"{R}/{rel}"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "wb").write(data)
            local.append(rel)
        except Exception:
            # keep remote URL as fallback so page still works until URL rots
            local.append(url)
    if local:
        item["media"] = local
        item["media_archived"] = True

def age_days(item, now=None):
    now = now or now_utc()
    dt = parse_dt(item.get("created_at") or item.get("added_at"))
    if not dt:
        return 0
    return (now - dt).total_seconds() / 86400.0

def apply_aging(items):
    """Non-fav >30d: strip media. Fav >30d: ensure images archived (already downloaded on fav)."""
    now = now_utc()
    for item in items:
        aged = age_days(item, now) > AGE_DAYS
        if not aged:
            continue
        if item.get("fav"):
            download_item_images(item)
        else:
            # keep text info + link; drop images
            if "media" in item:
                item["media"] = []
            item.pop("media_archived", None)

def rfc(s):
    try:
        return email.utils.format_datetime(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")))
    except Exception:
        return email.utils.format_datetime(now_utc())

def write_feed(name, title, its):
    x = [f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
         f"<title>{html.escape(title)}</title><link>{BASE}/</link><description>X 热帖精选与汽车、AI、机器人每日简报。</description><language>zh-cn</language>",
         f'<atom:link href="{BASE}/{name}" rel="self" type="application/rss+xml"/><lastBuildDate>{email.utils.format_datetime(now_utc())}</lastBuildDate><ttl>180</ttl>']
    for i in its[:RSS_LIMIT]:
        u = html.escape(item_link(i), quote=True)
        if is_brief(i):
            body = md_to_html(i.get("text") or "")
            body = body.replace("]]>", "]]&gt;")
            author = html.escape(i.get("name") or "每日简报")
        else:
            m = i.get("metrics", {})
            mt = " · ".join(f"{k} {v}" for k, v in (("赞", m.get("likes")), ("转", m.get("retweets")), ("浏览", m.get("views"))) if v is not None)
            imgs = ""
            for pth in (i.get("media") or [])[:4]:
                src = pth if str(pth).startswith("http") else f"{BASE}/{pth}"
                imgs += f'<p><img src="{html.escape(src)}"/></p>'
            body = f"<p>{html.escape(i['summary'])}</p><p>@{html.escape(i['user'])}{(' · '+mt) if mt else ''}</p>{imgs}<blockquote>{html.escape(i.get('text',''))}</blockquote><p><a href=\"{u}\">查看原帖</a></p>"
            author = "@" + html.escape(i['user'])
        x.append(f"<item><title>[{i['category']}] {html.escape(i['title'])}</title><link>{u}</link><guid isPermaLink=\"true\">{u}</guid><category>{i['category']}</category><author>{author}</author><pubDate>{rfc(i.get('created_at') or i['added_at'])}</pubDate><description><![CDATA[{body}]]></description></item>")
    x.append("</channel></rss>")
    open(f"{R}/{name}", "w").write("\n".join(x))

def card_static(i, img_prefix=""):
    """Server-rendered card HTML for archive pages (fav always ★)."""
    m = i.get("metrics") or {}
    media = i.get("media") or []
    img = media[0] if media else ""
    if img and not str(img).startswith("http"):
        img = img_prefix + img
    def esc(s):
        return html.escape(str(s or ""))
    def fmt(n):
        if n is None: return ""
        if n >= 1e6: return f"{n/1e6:.1f}M"
        if n >= 1e3: return f"{n/1e3:.1f}k"
        return str(n)
    dt = parse_dt(i.get("created_at") or i.get("added_at"))
    when = dt.strftime("%Y-%m-%d") if dt else ""
    stats = ""
    if m.get("likes") is not None: stats += f"<span>♥ {fmt(m['likes'])}</span>"
    if m.get("retweets") is not None: stats += f"<span>⇄ {fmt(m['retweets'])}</span>"
    if m.get("views") is not None: stats += f"<span>◉ {fmt(m['views'])}</span>"
    img_html = f'<img loading="lazy" src="{esc(img)}" alt="" onerror="this.remove()">' if img else ""
    href = item_link(i)
    who = "每日简报" if is_brief(i) else f'@{esc(i["user"])}'
    mark = '<span class="brief-mark">简报</span>' if is_brief(i) else ""
    fav_btn = "" if is_brief(i) else '<span class="fav-btn on" title="已收藏">★</span>'
    fav_meta = "" if is_brief(i) else '<span title="已收藏">★</span>'
    return (
        f'<a class="card" href="{esc(href)}" target="_blank" rel="noopener">'
        f'{fav_btn}'
        f'{img_html}<div class="body"><div class="meta"><span class="tag t-{esc(i["category"])}">{esc(i["category"])}</span>{mark}'
        f'<span>{when}</span>{fav_meta}</div>'
        f'<h2>{esc(i["title"])}</h2><p>{esc(i["summary"])}</p>'
        f'<div class="foot"><span class="who">{who}</span><span class="stats">{stats}</span></div></div></a>'
    )

def write_archive_pages(items):
    now = now_utc()
    fav_aged = []
    text_aged = []
    for i in items:
        aged = age_days(i, now) > AGE_DAYS
        if not aged:
            continue
        if i.get("fav"):
            fav_aged.append(i)
        else:
            text_aged.append(i)

    # group fav by YYYY-MM of created_at
    months = {}
    for i in fav_aged:
        dt = parse_dt(i.get("created_at") or i.get("added_at")) or now
        key = dt.strftime("%Y-%m")
        months.setdefault(key, []).append(i)

    for key, its in months.items():
        its.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        cards = "\n".join(card_static(i, img_prefix="../") for i in its)
        # Fix local image paths: media is archive/img/... from repo root; from archive/YYYY-MM.html need ../archive/img or just ../ for archive/
        # card_static with img_prefix="../" turns "archive/img/x.jpg" into "../archive/img/x.jpg" — correct from archive/YYYY-MM.html
        body = f'<main id="feed">\n{cards}\n</main>'
        html_out = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>收藏归档 · {key}</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<header><div class="bar">
<h1>收藏归档 · {key}<small>{len(its)} 篇</small></h1>
<div class="navlinks">
<a href="../index.html">首页</a>
<a href="index.html">归档首页</a>
<a href="text.html">更早内容</a>
</div>
</div></header>
<main id="feed">
{cards}
</main>
<footer>内容整理自 X 公开帖子，版权归原作者所有</footer>
</body></html>
"""
        open(f"{R}/archive/{key}.html", "w").write(html_out)

    # archive/index.html
    month_keys = sorted(months.keys(), reverse=True)
    # also discover existing month pages
    for fn in os.listdir(f"{R}/archive"):
        if re.match(r"^\d{4}-\d{2}\.html$", fn):
            k = fn[:-5]
            if k not in months:
                month_keys.append(k)
    month_keys = sorted(set(month_keys), reverse=True)
    lis = []
    for k in month_keys:
        n = len(months.get(k, []))
        # count from file if not in this build's months
        if n == 0:
            # leave count blank-ish
            lis.append(f'<li><a href="{k}.html"><span>{k}</span><span>查看 →</span></a></li>')
        else:
            lis.append(f'<li><a href="{k}.html"><span>{k}</span><span>{n} 篇 →</span></a></li>')
    if not lis:
        lis.append('<li style="color:var(--sub);padding:20px 0">暂无已归档收藏（收藏满 30 天后会出现在这里）</li>')
    open(f"{R}/archive/index.html", "w").write(f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>收藏归档</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<header><div class="bar">
<h1>收藏归档</h1>
<div class="navlinks">
<a href="../index.html">首页</a>
<a href="text.html">更早内容</a>
</div>
</div></header>
<div class="page">
<p style="color:var(--sub);margin-bottom:16px">被收藏且超过 30 天的帖子按月归档，图片已本地备份。</p>
<ul class="month-list">
{''.join(lis)}
</ul>
</div>
<footer>内容整理自 X 公开帖子，版权归原作者所有</footer>
</body></html>
""")

    # archive/text.html — non-fav >30d text-only
    text_aged.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    groups = {}
    for i in text_aged:
        dt = parse_dt(i.get("created_at") or i.get("added_at")) or now
        groups.setdefault(dt.strftime("%Y-%m"), []).append(i)
    parts = []
    for k in sorted(groups.keys(), reverse=True):
        parts.append(f"<h2>{k}</h2>")
        for i in groups[k]:
            dt = parse_dt(i.get("created_at") or i.get("added_at"))
            day = dt.strftime("%m-%d") if dt else ""
            link = html.escape(item_link(i))
            who = "每日简报" if is_brief(i) else f"@{html.escape(i.get('user') or '')}"
            parts.append(
                f'<a class="text-row" href="{link}" target="_blank" rel="noopener">'
                f'<div class="meta"><span class="tag t-{html.escape(i["category"])}">{html.escape(i["category"])}</span>'
                f'<span>{day}</span></div>'
                f'<h3>{html.escape(i.get("title") or "")}</h3>'
                f'<div class="sum">{html.escape(i.get("summary") or "")}</div>'
                f'<div class="who">{who} · 查看原文</div></a>'
            )
    if not parts:
        parts.append('<p style="color:var(--sub);padding:40px 0;text-align:center">暂无更早内容</p>')
    open(f"{R}/archive/text.html", "w").write(f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>更早内容</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<header><div class="bar">
<h1>更早内容</h1>
<div class="navlinks">
<a href="../index.html">首页</a>
<a href="index.html">收藏归档</a>
</div>
</div></header>
<div class="page">
<p style="color:var(--sub);margin-bottom:16px">超过 30 天且未收藏的帖子仅保留文字与链接。</p>
{''.join(parts)}
</div>
<footer>内容整理自 X 公开帖子，版权归原作者所有</footer>
</body></html>
""")

CARD_KEYS = ("id", "kind", "user", "category", "title", "summary",
             "created_at", "added_at", "metrics", "media", "url", "fav")

def slim_card(i, archive_only=False):
    """Homepage cards only need title/summary; drop article body and empty fields."""
    row = {}
    for k in CARD_KEYS:
        v = i.get(k)
        if v in (None, "", [], {}):
            continue
        if k == "fav" and not v:
            continue
        row[k] = v
    if archive_only:
        row["_archive_only"] = True
    return row

def index_payload(items):
    """Items for main page: last 30 days + all fav (any age).
    Aged favs are marked _archive_only so category tabs hide them but 收藏 shows them.
    """
    now = now_utc()
    out = []
    for i in items:
        aged = age_days(i, now) > AGE_DAYS
        if not aged:
            out.append(slim_card(i))
        elif i.get("fav"):
            out.append(slim_card(i, archive_only=True))
    return out

def git_push(msg):
    run(["git", "-C", R, "add", "-A"])
    st = run(["git", "-C", R, "status", "--porcelain"])
    if not (st.stdout or "").strip():
        return 0, "noop"
    run(["git", "-C", R, "commit", "-qm", msg])
    # Rebase onto remote before push so box ↔ Actions do not conflict
    pl = run(["git", "-C", R, "pull", "--rebase", "origin", "main"])
    if pl.returncode != 0:
        run(["git", "-C", R, "rebase", "--abort"])
        return pl.returncode, ("pull --rebase failed: " + (pl.stderr or pl.stdout or ""))[-300:]
    p = run(["git", "-C", R, "push", "origin", "main"])
    return p.returncode, (p.stderr or p.stdout or "")[-300:]

def load_items():
    if os.path.exists(D):
        return json.load(open(D))
    return []

def save_items(items):
    json.dump(items, open(D, "w"), ensure_ascii=False, indent=1)

def merge_new(items, path):
    if not path or not os.path.exists(path):
        return items, 0
    try:
        raw = open(path).read().strip()
        if not raw:
            return items, 0
        incoming = json.loads(raw)
    except Exception:
        return items, 0
    if not isinstance(incoming, list):
        return items, 0
    seen = {i["id"] for i in items}
    now = now_utc().isoformat()
    new = []
    for i in incoming:
        if not i.get("id") or i["id"] in seen:
            continue
        row = dict(i)
        row.setdefault("added_at", now)
        new.append(row)
        seen.add(i["id"])
    return new + items, len(new)

def pick_rss(items, category=None):
    pool = [i for i in items if category is None or i.get("category") == category]
    return sort_items(list(pool))[:RSS_LIMIT]

def build_all(items):
    sort_items(items)
    write_brief_pages(items)
    write_feed("feed.xml", "X 热帖精选 · 汽车 / AI / 机器人", pick_rss(items))
    for k, c in CAT.items():
        write_feed(f"feed-{k}.xml", f"X 热帖精选 · {c}", pick_rss(items, c))
    write_archive_pages(items)
    payload = index_payload(items)
    # For main page JS: local media paths need no prefix (site root)
    upd = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M") + " 北京时间"
    tpl = open(f"{R}/index_template.html").read()
    html_out = (tpl
                .replace("__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
                .replace("__UPDATED__", upd)
                .replace("__BASE_PATH__", ""))
    open(f"{R}/index.html", "w").write(html_out)
    open(f"{R}/.nojekyll", "w").close()

def parse_cli(argv):
    args = list(argv)
    sync_only = "--sync-only" in args
    args = [a for a in args if a != "--sync-only"]
    brief_items = []
    new_path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--brief":
            if i + 2 >= len(args):
                raise SystemExit("usage: --brief <ai|auto|robot> <file.md>")
            brief_items.extend(make_brief_items(args[i + 1], args[i + 2]))
            i += 3
        elif a == "--briefs-dir":
            if i + 1 >= len(args):
                raise SystemExit("usage: --briefs-dir <dir>")
            brief_items.extend(collect_briefs_from_dir(args[i + 1]))
            i += 2
        elif a.startswith("-"):
            raise SystemExit(f"unknown flag: {a}")
        else:
            if new_path:
                raise SystemExit(f"unexpected extra argument: {a}")
            new_path = a
            i += 1
    return sync_only, new_path, brief_items

def main():
    sync_only, new_path, brief_items = parse_cli(sys.argv[1:])

    items = load_items()
    # Keep forever — no 300 cap
    applied, sync_err = sync_favorites(items)
    # Ensure all current favs have local images
    for i in items:
        if i.get("fav"):
            download_item_images(i)
    apply_aging(items)

    n_new = 0
    if not sync_only:
        items, n_new = merge_new(items, new_path)
        if brief_items:
            items = drop_replaced_briefs(items, brief_items)
            import tempfile
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(brief_items, tmp, ensure_ascii=False)
            tmp.close()
            items, n_brief = merge_new(items, tmp.name)
            n_new += n_brief
            os.unlink(tmp.name)
        # re-apply aging after merge
        apply_aging(items)
        sort_items(items)

    save_items(items)
    build_all(items)

    now = now_utc().isoformat()
    msg = f"update {now[:16]} +{n_new} fav={len(applied)}"
    if brief_items and n_new:
        dates = sorted({i.get("brief_date") for i in brief_items if i.get("brief_date")})
        span = f"{dates[0]}..{dates[-1]}" if len(dates) > 1 else (dates[0] if dates else "")
        msg = f"briefs +{n_new} {span}".strip()
    if sync_only and applied:
        msg = f"fav-sync {now[:16]} {applied}"
    elif sync_only:
        msg = f"sync-only {now[:16]}"
    rc, err = git_push(msg)
    print(json.dumps({
        "new": n_new,
        "total": len(items),
        "fav_applied": applied,
        "push_rc": rc,
        "err": err if rc else "",
        "sync_err": sync_err or "",
    }, ensure_ascii=False))

if __name__ == "__main__":
    main()
