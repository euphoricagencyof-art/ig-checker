"""Облачный чекер просмотров (GitHub Actions). Данные — в ПРИВАТНОМ репозитории DATA_REPO, в публичных логах ников нет.
Каждый запуск: читает accounts.txt -> замеряет профили в Chrome с сессией чекер-аккаунта -> пишет snaps/ДАТА/ЧЧММ.json и state.json
(пик просмотров по каждому Reel хранится навсегда) -> шлёт в Telegram новые баны / часовую сводку / аварии."""
import asyncio, base64, json, os, re, sys, time
import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

TOKEN = os.environ["DATA_TOKEN"]; REPO = os.environ["DATA_REPO"]
STATE_SESSION = json.loads(os.environ["IG_SESSION"])
TG = (os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT"))
TH_ALERT, TH_REPORT = os.environ.get("TG_THREAD_ALERT"), os.environ.get("TG_THREAD_REPORT")
TABS = 3
MIN_GAP = int(os.environ.get("MIN_GAP", "540"))        # не чаще, чем раз в 9 минут (дубли расписания отсекаем)
API = f"https://api.github.com/repos/{REPO}/contents"
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}


def gh_get(path):
    r = requests.get(f"{API}/{path}", headers=H, timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json()
    if j.get("content") is None or j.get("encoding") == "none":   # файл > 1 МБ
        raw = requests.get(j["download_url"], headers=H, timeout=60).content
        return raw, j["sha"]
    return base64.b64decode(j["content"]), j["sha"]


def gh_put(path, data, sha=None, msg="check"):
    body = {"message": msg, "content": base64.b64encode(data if isinstance(data, bytes) else data.encode()).decode()}
    if sha:
        body["sha"] = sha
    for i in range(3):
        r = requests.put(f"{API}/{path}", headers=H, json=body, timeout=60)
        if r.status_code in (200, 201):
            return True
        if r.status_code in (409, 422) and path == "state.json":
            _, body["sha"] = gh_get(path)
        time.sleep(2)
    print("gh_put fail", path, r.status_code)
    return False


def tg(text, thread):
    if not TG[0]:
        return
    try:
        d = {"chat_id": TG[1], "text": text[:3800]}
        if thread:
            d["message_thread_id"] = thread
        requests.post(f"https://api.telegram.org/bot{TG[0]}/sendMessage", data=d, timeout=20)
    except Exception as e:
        print("tg err", type(e).__name__)


def num(t):
    m = re.fullmatch(r"([\d.,]+)\s*([KkMm]?)", t.strip())
    return int(float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]) if m else None


async def read_profile(page, user):
    try:
        await page.goto(f"https://www.instagram.com/{user}/reels/", timeout=40000, wait_until="domcontentloaded")
        ready = re.compile(r"\bposts?\b|isn't available|Log in|Sign up|Something went wrong", re.I)
        for _ in range(40):
            body = (await page.inner_text("body"))[:600]
            if ready.search(body) or "/accounts/login" in page.url:
                break
            await page.wait_for_timeout(500)
        else:
            return {"st": "error"}
        await page.wait_for_timeout(1200)
    except Exception:
        return {"st": "error"}
    url = page.url
    body = (await page.inner_text("body"))[:800]
    if "/accounts/login" in url or ("Log in" in body[:300] and not re.search(r"\bposts?\b", body[:300])):
        return {"st": "session_dead"}
    if "challenge" in url:
        return {"st": "session_dead"}
    if "isn't available" in body[:400]:
        return {"st": "banned"}
    if "This account is private" in body.lower() or "this account is private" in body.lower():
        return {"st": "private"}
    m = re.search(r"([\d.,]+[KkMm]?)\s+posts?\b", body[:300])
    if not m:
        return {"st": "error"}
    posts = num(m.group(1)) or 0
    vids = []
    if posts:
        await page.wait_for_timeout(800)
        links = await page.eval_on_selector_all('a[href*="/reel/"]', "els=>els.map(e=>[e.getAttribute('href'),e.innerText.replace(/\\n/g,' ').trim()])")
        for href, txt in links:
            mm = re.search(r"/reel/([\w-]+)", href); v = num(txt.split()[0]) if txt else None
            if mm and v is not None:
                vids.append([mm.group(1), v])
    return {"st": "ok" if posts else "empty", "posts": posts, "v": vids}


async def run(users):
    out = {}; dead = False
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(storage_state=STATE_SESSION, viewport={"width": 1280, "height": 900}, locale="en-US",
                                  user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        await Stealth().apply_stealth_async(ctx)
        sem = asyncio.Semaphore(TABS)
        async def one(u):
            nonlocal dead
            async with sem:
                if dead:
                    return
                pg = await ctx.new_page()
                try:
                    r = await read_profile(pg, u)
                finally:
                    await pg.close()
                if r["st"] == "session_dead":
                    dead = True; return
                out[u] = r
        await asyncio.gather(*(one(u) for u in users))
        await b.close()
    return out, dead


def main():
    now = time.time()
    raw, sha = gh_get("state.json")
    state = json.loads(raw) if raw else {"ts": 0, "accounts": {}, "reels": {}, "last_report": 0, "alerts": {}}
    if now - state.get("ts", 0) < MIN_GAP:
        print("skip: замер свежий"); return
    ar, _ = gh_get("accounts.txt")
    users = [l.split()[0] for l in (ar or b"").decode().splitlines() if l.strip() and not l.startswith("#")]
    if not users:
        print("нет списка аккаунтов"); return
    t0 = time.time()
    res, dead = asyncio.run(run(users))
    if dead:
        if now - state["alerts"].get("session", 0) > 3 * 3600:
            tg("🛑 Облачный чекер: сессия чекер-аккаунта умерла (Instagram просит вход). Замеры стоят. Нужна новая сессия.", TH_ALERT)
            state["alerts"]["session"] = now
            gh_put("state.json", json.dumps(state, ensure_ascii=False), sha, "alert session")
        print("session dead"); sys.exit(1)
    errs = sum(1 for r in res.values() if r["st"] == "error")
    if errs > max(8, 0.25 * len(users)) and now - state["alerts"].get("errors", 0) > 2 * 3600:
        tg(f"⚠️ Облачный чекер: {errs} из {len(users)} аккаунтов не прочитались (возможно, Instagram режет запросы из облака).", TH_ALERT)
        state["alerts"]["errors"] = now
    new_bans = []
    for u, r in res.items():
        prev = state["accounts"].get(u, {})
        if r["st"] == "error":
            continue                                   # ошибка замера не меняет известный статус
        if r["st"] == "banned" and prev.get("st") != "banned":
            new_bans.append(u)
        state["accounts"][u] = {"st": r["st"], "posts": r.get("posts"), "ts": now,
                                "views": sum(v for _, v in r.get("v", [])), "since": prev.get("since") if prev.get("st") == r["st"] else now}
        seen = set()
        for code, v in r.get("v", []):
            seen.add(code)
            e = state["reels"].get(code) or {"u": u, "first": now, "max": 0}
            e.update({"last": now, "cur": v, "alive": 1, "max": max(e.get("max", 0), v)})
            state["reels"][code] = e
        for code, e in state["reels"].items():
            if e["u"] == u and e.get("alive") and code not in seen and r["st"] in ("ok", "empty"):
                e["alive"] = 0; e["gone"] = now
    state["ts"] = now
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    snap = {"ts": now, "r": {u: r for u, r in res.items()}}
    gh_put(f"snaps/{day}/{time.strftime('%H%M', time.gmtime(now))}.json", json.dumps(snap, ensure_ascii=False))
    if new_bans:
        peak = {u: sum(e["max"] for e in state["reels"].values() if e["u"] == u) for u in new_bans}
        tg("🚫 Новые баны: %d\n%s" % (len(new_bans), "\n".join(f"• {u} — пик {peak[u]:,}".replace(",", " ") for u in new_bans[:25])), TH_ALERT)
    if now - state.get("last_report", 0) >= 3500:
        st = {}
        for a in state["accounts"].values():
            st[a["st"]] = st.get(a["st"], 0) + 1
        reels = list(state["reels"].values())
        tot = sum(e["max"] for e in reels)
        top = sorted(state["reels"].items(), key=lambda kv: -kv[1]["max"])[:5]
        gone = sum(1 for e in reels if not e.get("alive"))
        tg("📊 Облачный чекер · %s\nАккаунтов %d: живы с роликами %d · без постов %d · баны %d\nПросмотры (ПИК по всем роликам): %s\nРоликов %d, удалено/пропало %d\nТоп-5: %s" % (
            time.strftime("%H:%M", time.gmtime(now + 3600 * 2)), len(state["accounts"]), st.get("ok", 0), st.get("empty", 0), st.get("banned", 0),
            f"{tot:,}".replace(",", " "), len(reels), gone,
            ", ".join(f"{e['u']} {e['max']:,}".replace(",", " ") for _, e in top)), TH_REPORT)
        state["last_report"] = now
    gh_put("state.json", json.dumps(state, ensure_ascii=False), sha, "state")
    print(f"ok: {len(res)} аккаунтов за {time.time() - t0:.0f} с, ошибок {errs}, новых банов {len(new_bans)}")


main()
