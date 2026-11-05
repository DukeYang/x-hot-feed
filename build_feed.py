#!/usr/bin/env python3
"""build_feed.py <new_items.json>  — 合并新条目到 data/items.json，生成 feed.xml（全部）、feed-auto/ai/robot.xml、index.html，然后 git push。
new_items.json: [{"id":tweet_id,"user":handle,"name":显示名,"category":"汽车|AI|机器人","title":中文标题,"summary":中文概括,"text":原文,"created_at":ISO,"metrics":{"likes":..,"retweets":..,"views":..},"media":[url..]}]"""
import sys,json,os,html,subprocess,datetime,email.utils
R="/home/box/x-hot-feed"; D=f"{R}/data/items.json"; os.makedirs(f"{R}/data",exist_ok=True)
old=json.load(open(D)) if os.path.exists(D) else []
seen={i["id"] for i in old}; now=datetime.datetime.now(datetime.timezone.utc).isoformat()
new=[dict(i,added_at=now) for i in json.load(open(sys.argv[1])) if i["id"] not in seen]
items=(new+old)[:300]; json.dump(items,open(D,"w"),ensure_ascii=False,indent=1)
BASE="https://dukeyang.github.io/x-hot-feed"
CAT={"auto":"汽车","ai":"AI","robot":"机器人"}
def rfc(s):
    try: return email.utils.format_datetime(datetime.datetime.fromisoformat(s.replace("Z","+00:00")))
    except Exception: return email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
def feed(name,title,its):
    x=[f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
       f"<title>{html.escape(title)}</title><link>{BASE}/</link><description>每 3 小时整理的 X（推特）汽车、AI、机器人热帖，中文概括。</description><language>zh-cn</language>",
       f'<atom:link href="{BASE}/{name}" rel="self" type="application/rss+xml"/><lastBuildDate>{email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))}</lastBuildDate><ttl>180</ttl>']
    for i in its[:100]:
        u=f"https://x.com/{i['user']}/status/{i['id']}"; m=i.get("metrics",{})
        mt=" · ".join(f"{k} {v}" for k,v in (("赞",m.get("likes")),("转",m.get("retweets")),("浏览",m.get("views"))) if v is not None)
        imgs="".join(f'<p><img src="{html.escape(p)}"/></p>' for p in i.get("media",[])[:4])
        body=f"<p>{html.escape(i['summary'])}</p><p>@{html.escape(i['user'])}{(' · '+mt) if mt else ''}</p>{imgs}<blockquote>{html.escape(i.get('text',''))}</blockquote><p><a href=\"{u}\">查看原帖</a></p>"
        x.append(f"<item><title>[{i['category']}] {html.escape(i['title'])}</title><link>{u}</link><guid isPermaLink=\"true\">{u}</guid><category>{i['category']}</category><author>@{html.escape(i['user'])}</author><pubDate>{rfc(i.get('created_at') or i['added_at'])}</pubDate><description><![CDATA[{body}]]></description></item>")
    x.append("</channel></rss>"); open(f"{R}/{name}","w").write("\n".join(x))
feed("feed.xml","X 热帖精选 · 汽车 / AI / 机器人",items)
for k,c in CAT.items(): feed(f"feed-{k}.xml",f"X 热帖精选 · {c}",[i for i in items if i["category"]==c])
upd=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")+" 北京时间"
open(f"{R}/index.html","w").write(open(f"{R}/index_template.html").read().replace("__DATA__",json.dumps(items[:150],ensure_ascii=False).replace("</","<\\/")).replace("__UPDATED__",upd))
open(f"{R}/.nojekyll","w").close()
subprocess.run(["git","-C",R,"add","-A"]); 
c=subprocess.run(["git","-C",R,"commit","-qm",f"update {now[:16]} +{len(new)}"],capture_output=True,text=True)
p=subprocess.run(["git","-C",R,"push","-q","origin","main"],capture_output=True,text=True)
print(json.dumps({"new":len(new),"total":len(items),"push_rc":p.returncode,"err":p.stderr[-200:]},ensure_ascii=False))
