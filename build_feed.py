#!/usr/bin/env python3
"""build_feed.py [new_items.json | --sync-only]
合并新条目、同步收藏 Issue、应用 30 天老化规则、生成 RSS/首页/归档页，然后 git push。
"""
import sys, json, os, html, subprocess, datetime, email.utils, re, urllib.request, urllib.error, shutil, pathlib

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
         f"<title>{html.escape(title)}</title><link>{BASE}/</link><description>每 3 小时整理的 X（推特）汽车、AI、机器人热帖，中文概括。</description><language>zh-cn</language>",
         f'<atom:link href="{BASE}/{name}" rel="self" type="application/rss+xml"/><lastBuildDate>{email.utils.format_datetime(now_utc())}</lastBuildDate><ttl>180</ttl>']
    for i in its[:RSS_LIMIT]:
        u = f"https://x.com/{i['user']}/status/{i['id']}"
        m = i.get("metrics", {})
        mt = " · ".join(f"{k} {v}" for k, v in (("赞", m.get("likes")), ("转", m.get("retweets")), ("浏览", m.get("views"))) if v is not None)
        # RSS: prefer remote-looking URLs; local paths get absolute site URL
        imgs = ""
        for p in (i.get("media") or [])[:4]:
            src = p if str(p).startswith("http") else f"{BASE}/{p}"
            imgs += f'<p><img src="{html.escape(src)}"/></p>'
        body = f"<p>{html.escape(i['summary'])}</p><p>@{html.escape(i['user'])}{(' · '+mt) if mt else ''}</p>{imgs}<blockquote>{html.escape(i.get('text',''))}</blockquote><p><a href=\"{u}\">查看原帖</a></p>"
        x.append(f"<item><title>[{i['category']}] {html.escape(i['title'])}</title><link>{u}</link><guid isPermaLink=\"true\">{u}</guid><category>{i['category']}</category><author>@{html.escape(i['user'])}</author><pubDate>{rfc(i.get('created_at') or i['added_at'])}</pubDate><description><![CDATA[{body}]]></description></item>")
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
    return (
        f'<a class="card" href="https://x.com/{esc(i["user"])}/status/{esc(i["id"])}" target="_blank" rel="noopener">'
        f'<span class="fav-btn on" title="已收藏">★</span>'
        f'{img_html}<div class="body"><div class="meta"><span class="tag t-{esc(i["category"])}">{esc(i["category"])}</span>'
        f'<span>{when}</span><span title="已收藏">★</span></div>'
        f'<h2>{esc(i["title"])}</h2><p>{esc(i["summary"])}</p>'
        f'<div class="foot"><span class="who">@{esc(i["user"])}</span><span class="stats">{stats}</span></div></div></a>'
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
            link = f"https://x.com/{html.escape(i['user'])}/status/{html.escape(i['id'])}"
            parts.append(
                f'<a class="text-row" href="{link}" target="_blank" rel="noopener">'
                f'<div class="meta"><span class="tag t-{html.escape(i["category"])}">{html.escape(i["category"])}</span>'
                f'<span>{day}</span></div>'
                f'<h3>{html.escape(i.get("title") or "")}</h3>'
                f'<div class="sum">{html.escape(i.get("summary") or "")}</div>'
                f'<div class="who">@{html.escape(i.get("user") or "")} · 查看原帖</div></a>'
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

def index_payload(items):
    """Items for main page: last 30 days + all fav (any age). Mark aged fav as _archive_only=False still shown in 收藏 tab.
    For category tabs, exclude items older than 30 days unless viewing 收藏 (client filters).
    Pass _archive_only=True for fav items older than 30d so category tabs hide them but 收藏 tab shows them.
    """
    now = now_utc()
    out = []
    for i in items:
        aged = age_days(i, now) > AGE_DAYS
        if not aged:
            row = dict(i)
            row.pop("_archive_only", None)
            out.append(row)
        elif i.get("fav"):
            row = dict(i)
            row["_archive_only"] = True  # hide from 全部/分类, show in 收藏
            out.append(row)
        # non-fav aged: not on main page
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

def build_all(items):
    write_feed("feed.xml", "X 热帖精选 · 汽车 / AI / 机器人", items)
    for k, c in CAT.items():
        write_feed(f"feed-{k}.xml", f"X 热帖精选 · {c}", [i for i in items if i["category"] == c])
    write_archive_pages(items)
    payload = index_payload(items)
    # For main page JS: local media paths need no prefix (site root)
    upd = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M") + " 北京时间"
    tpl = open(f"{R}/index_template.html").read()
    html_out = (tpl
                .replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
                .replace("__UPDATED__", upd)
                .replace("__BASE_PATH__", ""))
    open(f"{R}/index.html", "w").write(html_out)
    open(f"{R}/.nojekyll", "w").close()

def main():
    args = sys.argv[1:]
    sync_only = "--sync-only" in args
    args = [a for a in args if a != "--sync-only"]
    new_path = args[0] if args else None

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
        # re-apply aging after merge
        apply_aging(items)

    save_items(items)
    build_all(items)

    now = now_utc().isoformat()
    msg = f"update {now[:16]} +{n_new} fav={len(applied)}"
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
