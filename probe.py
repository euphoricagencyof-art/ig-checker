import asyncio, json, os, re, time
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
users = os.environ["PROBE_USERS"].split(",")
state = json.loads(os.environ["IG_SESSION"])
def num(t):
    m = re.fullmatch(r"([\d.,]+)\s*([KkMm]?)", t.strip())
    return int(float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]) if m else None
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(storage_state=state, viewport={"width": 1280, "height": 900}, locale="en-US",
                                  user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        await Stealth().apply_stealth_async(ctx)
        for i, u in enumerate(users):
            pg = await ctx.new_page(); t0 = time.time(); res = "?"
            try:
                await pg.goto(f"https://www.instagram.com/{u}/reels/", timeout=40000, wait_until="domcontentloaded")
                for _ in range(40):
                    body = (await pg.inner_text("body"))[:600]
                    if re.search(r"\bposts?\b|isn't available|Log in|Sign up|Something went wrong", body, re.I) or "/accounts/login" in pg.url: break
                    await pg.wait_for_timeout(500)
                url = pg.url
                if "/accounts/login" in url or re.search(r"Log in|Sign up", body[:300]) and "posts" not in body[:300]: res = "СЕССИЯ ВЫКИНУТА (логин-страница)"
                elif "challenge" in url or "suspended" in url: res = "ЧЕЛЛЕНДЖ: " + url.split("/")[3]
                elif "isn't available" in body[:400]: res = "страница недоступна (бан/нет)"
                else:
                    m = re.search(r"([\d.,]+[KkMm]?)\s+posts?\b", body)
                    links = await pg.eval_on_selector_all('a[href*="/reel/"]', "els=>els.map(e=>e.innerText.replace(/\\n/g,' ').trim())")
                    res = f"OK posts={m.group(1) if m else '?'} views={[num(x.split()[0]) for x in links if x]}"
            except Exception as e:
                res = "ОШИБКА " + str(e)[:80]
            print(f"#{i+1}: {res} ({time.time()-t0:.1f}s)", flush=True)
            await pg.close()
        await b.close()
asyncio.run(main())
