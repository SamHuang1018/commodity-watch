"""
AirPods Pro 3 降價監控（momo + PChome）
- PChome：用官方前台 JSON API（搜尋 + 即時價格/庫存）
- momo：用官網搜尋頁背後的 JSON API（商品頁有 Akamai 防爬，搜尋 API 沒有）
- 兩邊都不用開瀏覽器
- 通知：ntfy.sh 推播到手機（免註冊、免介面）

用法：
    python airpods_watch.py              # 持續監控（預設每 10 分鐘）
    python airpods_watch.py --once       # 只跑一次（給 Windows 工作排程器用）
    python airpods_watch.py --test       # 送一則測試推播，確認手機收得到

在 GitHub Actions 上跑：ntfy 主題從環境變數 NTFY_TOPIC（GitHub Secret）讀，
狀態存在 ci_state.json 並由 workflow commit 回 repo。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import requests

# ========================= 設定 =========================
# ntfy 主題名稱：優先讀環境變數 NTFY_TOPIC；都沒有的話本機會自動產生一個隨機名稱存起來
# （ntfy.sh 的主題是公開的，名稱越難猜越好）
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
NTFY_SERVER = "https://ntfy.sh"

# 目標價（選填）：價格 <= 這個數字時通知會標成高優先（手機會響比較大聲）；None = 不設
TARGET_PRICE: int | None = None

CHECK_INTERVAL_MIN = 10          # 持續模式的檢查間隔（分鐘）

# 兩個平台都用關鍵字搜尋，自動抓所有 AirPods Pro 3 的賣場
KEYWORD = "airpods pro 3"

# 商品名稱過濾：必須包含 / 不能包含
# 「保護套組」「清潔組」這類是本體 + 配件的組合，一樣是全新 AirPods，所以保留
NAME_MUST = "airpods pro 3"
NAME_EXCLUDE = ["福利品", "整新", "二手", "展示", "拆封", "福利機"]
PRICE_RANGE = (5000, 9500)       # 合理本體價格區間，順便排除單買的保護殼、耳塞等配件

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / os.environ.get("STATE_FILE", "state.json")
LOG_FILE = BASE_DIR / "price_log.csv"
FAIL_ALERT_AFTER = 6             # 某個平台連續幾輪抓不到價格，就推播提醒「監控可能壞了」
# ========================================================

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


@dataclass
class Listing:
    platform: str      # "PChome" / "momo"
    pid: str
    name: str
    price: int
    buyable: bool      # 現在買得到（有庫存、已開賣）
    status: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.pid}"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def name_ok(name: str) -> bool:
    n = name.lower()
    return NAME_MUST in n and not any(w in name for w in NAME_EXCLUDE)


# ------------------------- PChome -------------------------
def fetch_pchome() -> list[Listing]:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://24h.pchome.com.tw/"})

    # 1) 搜尋找出候選賣場
    #    用 price 參數讓 PChome 先濾掉配件，只剩 10 筆左右本體賣場，全部頁面都掃
    #   （不加的話有 260 多筆，只看前幾頁會因為排序變動漏掉賣場）
    candidates: dict[str, str] = {}
    page, total_pages = 1, 1
    while page <= min(total_pages, 10):
        r = s.get("https://ecshweb.pchome.com.tw/search/v4.3/all/results",
                  params={"q": KEYWORD, "page": page, "sort": "sale/dc",
                          "price": f"{PRICE_RANGE[0]}-{PRICE_RANGE[1]}"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        total_pages = int(data.get("TotalPage") or 1)
        page += 1
        for p in data.get("Prods") or []:
            name, price = p.get("Name", ""), p.get("Price") or 0
            if name_ok(name) and PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
                candidates[p["Id"]] = name
    if not candidates:
        return []

    # 2) 用 button API 拿即時價格與庫存（限時瘋搶的價格會反映在這裡）
    ids, rows = list(candidates), []
    for i in range(0, len(ids), 20):             # 一次最多查 20 個
        r = s.get("https://ecapi-cdn.pchome.com.tw/ecshop/prodapi/v2/prod/button"
                  f"&id={','.join(ids[i:i + 20])}&fields=Id,Price,Qty,ButtonType", timeout=15)
        r.raise_for_status()
        rows += r.json()
    out = []
    for it in rows:
        pid = it["Id"].removesuffix("-000")
        pr = it.get("Price") or {}
        prices = [v for v in (pr.get("P"), pr.get("Low")) if isinstance(v, int) and v > 0]
        if not prices:
            continue
        btn = it.get("ButtonType", "")
        out.append(Listing(
            platform="PChome", pid=pid, name=candidates.get(pid, pid).strip(),
            price=min(prices), buyable=(btn == "ForSale" and (it.get("Qty") or 0) > 0),
            status=btn, url=f"https://24h.pchome.com.tw/prod/{pid}",
        ))
    return out


# -------------------------- momo --------------------------
MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HEADERS = {
    "User-Agent": UA,
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}


def _momo_post(payload: dict) -> dict:
    """先用 requests；若被擋（403 / 非 JSON）且有裝 curl_cffi，改用模擬 Chrome 的連線再試一次"""
    try:
        r = requests.post(MOMO_API, json=payload, headers=MOMO_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as first_err:
        try:
            from curl_cffi import requests as creq
        except ImportError:
            raise first_err
        r = creq.post(MOMO_API, json=payload, headers=MOMO_HEADERS, impersonate="chrome", timeout=15)
        r.raise_for_status()
        return r.json()


def fetch_momo() -> list[Listing]:
    out, seen = [], set()
    for page in (1, 2):
        payload = {"host": "momoshop", "flag": "searchEngine", "data": {
            "searchValue": KEYWORD, "searchType": "1", "currentPage": str(page),
            "priceS": str(PRICE_RANGE[0]), "priceE": str(PRICE_RANGE[1]),
            "cateCode": "", "cateLevel": "-1", "isFuzzy": "0", "showType": "chessboardType"}}
        data = (_momo_post(payload).get("rtnSearchData") or {})
        for g in data.get("goodsInfoList") or []:
            pid, name = str(g.get("goodsCode", "")), g.get("goodsName", "")
            price = int(re.sub(r"[^\d]", "", str(g.get("SALE_PRICE") or g.get("goodsPrice") or "")) or 0)
            if not pid or pid in seen or not price or not name_ok(name):
                continue
            if not (PRICE_RANGE[0] <= price <= PRICE_RANGE[1]):
                continue
            seen.add(pid)
            stock = int(re.sub(r"[^\d]", "", str(g.get("goodsStock") or "0")) or 0)
            out.append(Listing("momo", pid, name.strip(), price, stock > 0,
                               f"庫存{stock}" if stock else "售完",
                               f"https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code={pid}"))
        if int(data.get("maxPage") or 1) <= page:
            break
    return out


# ------------------------- 通知 -------------------------
def notify(topic: str, title: str, message: str, click: str | None = None, high: bool = False) -> None:
    payload = {"topic": topic, "title": title, "message": message,
               "tags": ["headphones"], "priority": 5 if high else 4}
    if click:
        payload["click"] = click
    try:
        requests.post(NTFY_SERVER, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        log(f"推播失敗：{e}")
    log(f"[通知] {title} | {message.replace(chr(10), ' / ')}")


# ------------------------- 狀態 -------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_topic(state: dict) -> str:
    if NTFY_TOPIC:
        return NTFY_TOPIC
    if IN_CI:
        raise SystemExit("找不到 NTFY_TOPIC：請到 GitHub repo 的 Settings > Secrets and variables > Actions 新增")
    if not state.get("ntfy_topic"):
        state["ntfy_topic"] = f"airpods-{secrets.token_hex(6)}"
        save_state(state)
    return state["ntfy_topic"]


def append_log(items: list[Listing]) -> None:
    if IN_CI:          # 雲端每次都是新環境，不留 CSV 紀錄（價格變化看 ci_state.json 的 commit 歷史）
        return
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", encoding="utf-8-sig") as f:
        if new:
            f.write("time,platform,id,price,buyable,status,name\n")
        now = datetime.now().isoformat(timespec="seconds")
        for it in items:
            f.write(f'{now},{it.platform},{it.pid},{it.price},{it.buyable},{it.status},"{it.name}"\n')


def fmt(it: Listing) -> str:
    return f"{it.platform} ${it.price:,}（{'可買' if it.buyable else it.status}）{it.name}"


# ------------------------- 主流程 -------------------------
def check_once(state: dict, topic: str) -> None:
    items: list[Listing] = []
    counts: dict[str, int] = {}
    fails = state.setdefault("fail", {})
    state.pop("fail_count", None)                # 舊版欄位
    for name, fn in (("PChome", fetch_pchome), ("momo", fetch_momo)):
        try:
            got = fn()
            items += got
            log(f"{name}: {len(got)} 筆")
        except Exception as e:
            got = []
            log(f"{name} 失敗：{type(e).__name__}: {e}")
        counts[name] = len(got)
        # 個別平台連續抓不到就提醒一次（例如被擋），恢復後歸零
        fails[name] = 0 if got else fails.get(name, 0) + 1
        if fails[name] == FAIL_ALERT_AFTER:
            notify(topic, f"AirPods 監控：{name} 抓不到價格",
                   f"{name} 已連續 {FAIL_ALERT_AFTER} 輪抓不到任何價格，可能被擋或網站改版，請檢查。")

    if not items:
        save_state(state)
        return
    append_log(items)
    for it in sorted(items, key=lambda x: x.price):
        log("  " + fmt(it))

    buyable = [it for it in items if it.buyable]
    if not buyable:
        log("目前沒有可購買的賣場")
        save_state(state)
        return

    best = min(buyable, key=lambda x: x.price)
    last_best = state.get("last_best")          # 上一輪最低（可買）價
    lowest = state.get("lowest")                # 歷史最低
    hit_target = TARGET_PRICE is not None and best.price <= TARGET_PRICE

    if last_best is None and lowest is None:
        where = "雲端 GitHub Actions" if IN_CI else "這台電腦"
        got = "、".join(f"{k} {v} 筆" for k, v in counts.items())
        notify(topic, f"開始監控 AirPods Pro 3：目前最低 ${best.price:,}",
               fmt(best) + f"\n執行位置：{where}（{got}）\n之後比現在更便宜才會通知。", click=best.url)
    elif last_best is None or best.price < last_best:
        is_record = lowest is None or best.price < lowest["price"]
        head = "歷史新低" if is_record else "降價"
        before = f"（原本 ${last_best:,}）" if last_best else ""
        notify(topic, f"{head}！AirPods Pro 3 ${best.price:,}",
               fmt(best) + f"\n{before}" + ("\n已達目標價" if hit_target else ""),
               click=best.url, high=hit_target or is_record)
    else:
        log(f"沒有更便宜：目前最低 ${best.price:,}（上一輪 ${last_best:,}）")

    state["last_best"] = best.price
    if lowest is None or best.price < lowest["price"]:
        state["lowest"] = {"price": best.price, "where": fmt(best), "url": best.url,
                           "at": datetime.now().isoformat(timespec="seconds")}
    state["latest"] = [asdict(it) for it in items]
    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只檢查一次就結束")
    ap.add_argument("--test", action="store_true", help="送一則測試推播")
    args = ap.parse_args()

    state = load_state()
    topic = get_topic(state)
    if IN_CI:
        log("ntfy 主題：（由 GitHub Secret 提供，不顯示）")
    else:
        log(f"ntfy 主題：{topic}（手機 ntfy App 訂閱這個名稱）")

    if args.test:
        notify(topic, "測試推播", "收到這則就代表設定成功 🎧")
        return
    if args.once:
        check_once(state, topic)
        return
    while True:
        check_once(state, topic)
        time.sleep(CHECK_INTERVAL_MIN * 60)


if __name__ == "__main__":
    main()
