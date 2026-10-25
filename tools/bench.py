import argparse
import csv
import json
import os
import random
import sys
import threading
import time
import uuid

import httpx

BASE = os.getenv("MONDJE_URL", "http://localhost:8000")
WORDS = ["cacao", "cafe", "engrais", "semences", "salaire", "loyer", "transport", "vente", "achat", "credit"]
WORK = os.getenv("BENCH_DIR", "bench_files")


def client():
    return httpx.Client(base_url=BASE, timeout=900)


def fresh_base():
    return random.getrandbits(100)


def make_file(name, n, base, bad=0.0):
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, name)
    rng = random.Random(base)
    period = round(1 / bad) if bad else 0
    broken = 0
    with open(path, "wb") as out:
        for i in range(n):
            uid = str(uuid.UUID(int=base + i))
            kind = rng.choice(["CREDIT", "DEBIT"])
            amount = rng.randint(100, 500000)
            desc = f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i}"
            line = f"{uid};{kind};{amount};{desc}".encode()
            if period and i % period == period - 1:
                broken += 1
                variant = rng.randrange(5)
                if variant == 0:
                    line = f"{uid};{kind};-{amount};{desc}".encode()
                elif variant == 1:
                    line = f"{uid};VIREMENT;{amount};{desc}".encode()
                elif variant == 2:
                    line = f"{uid[:-4]}zzzz;{kind};{amount};{desc}".encode()
                elif variant == 3:
                    line = f"{uid};{kind[:2]}".encode()
                else:
                    line = f"{uid};{kind};{amount};caf".encode() + b"\xe9" + b"s"
            out.write(line + b"\n")
    return path, broken


def chunks(path):
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                return
            yield block


def upload(c, path, agency=""):
    r = c.post("/api/imports", params={"filename": os.path.basename(path), "agency": agency},
               content=chunks(path), headers={"content-type": "application/octet-stream"})
    r.raise_for_status()
    return r.json()


def wait(c, import_id, tick=None):
    while True:
        data = c.get(f"/api/imports/{import_id}").json()
        if tick:
            tick(data)
        if data["status"] != "running":
            return data
        time.sleep(0.5)


def report(c, challenge, label, duration_ms=None, ok=None, errors=None, detail=None):
    payload = {"challenge": challenge, "label": label, "duration_ms": duration_ms, "rows_ok": ok,
               "errors_reported": errors, "detail": detail}
    c.post("/api/bench/results", json=payload).raise_for_status()
    shown = f"{duration_ms:.0f} ms" if duration_ms is not None else "-"
    print(f"[{challenge}] {label}: {shown} ok={ok} erreurs={errors} {detail or ''}")


def run_import(c, challenge, label, n, bad=0.0, agency=""):
    base = fresh_base()
    path, broken = make_file(f"import_{challenge}.txt", n, base, bad)
    started = time.perf_counter()
    data = upload(c, path, agency)
    final = wait(c, data["id"])
    elapsed = (time.perf_counter() - started) * 1000
    return base, path, broken, final, elapsed


def t_import(c):
    base, path, broken, final, elapsed = run_import(c, 2, "Import au chrono", 100000)
    report(c, 2, "Import au chrono (100 000 lignes)", elapsed, final["inserted"], final["errors"],
           f"durée serveur {final['duration_ms']:.0f} ms")
    k = random.randrange(100000)
    target = str(uuid.UUID(int=base + k))
    started = time.perf_counter()
    source = c.get(f"/api/transactions/{target}/source").json()
    elapsed = (time.perf_counter() - started) * 1000
    good = source.get("source_line") == k + 1 and source.get("filename") == os.path.basename(path)
    report(c, 7, "Qui a envoyé quoi ?", elapsed, 1 if good else 0, 0 if good else 1,
           f"{source.get('filename')} ligne {source.get('source_line')}")


def t_errors(c):
    base, path, broken, final, elapsed = run_import(c, 4, "Import qui ne s'arrête jamais", 50000, bad=0.02)
    listed = 0
    cursor = None
    first = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = c.get(f"/api/imports/{final['id']}/errors", params=params).json()
        listed += len(page["items"])
        first = first or (page["items"][0] if page["items"] else None)
        cursor = page["next_cursor"]
        if not cursor:
            break
    report(c, 4, "Import avec lignes cassées (50 000 lignes, 2 %)", elapsed, final["inserted"], listed,
           f"attendues {broken}, statut {final['status']}")
    if first:
        ctx = c.get(f"/api/imports/{final['id']}/errors/{first['id']}/context").json()
        ok = ctx["line"]["n"] == first["line_no"]
        report(c, 5, "Clic sur l'erreur", None, 1 if ok else 0, 0 if ok else 1, f"ligne {ctx['line']['n']}")


def t_parallel(c):
    base = fresh_base()
    specs = [("douala", 0), ("yaounde", 20000), ("bafoussam", 40000)]
    paths = [make_file(f"par_{a}.txt", 30000, base + off)[0] for a, off in specs]
    ids = {}
    ticks = []
    started = time.perf_counter()

    def send(agency, path):
        ids[agency] = upload(httpx.Client(base_url=BASE, timeout=900), path, agency)["id"]

    threads = [threading.Thread(target=send, args=(a, p)) for (a, _), p in zip(specs, paths)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    finals = [wait(c, ids[a], lambda d: ticks.append((d["filename"], d["bytes_offset"]))) for a, _ in specs]
    elapsed = (time.perf_counter() - started) * 1000
    unique = 70000
    inserted = sum(f["inserted"] for f in finals)
    report(c, 6, "Trois agences, trois fichiers", elapsed, inserted, sum(f["errors"] for f in finals),
           f"attendu {unique} uniques, doublons ignorés {sum(f['duplicates'] for f in finals)}")
    single = make_file("double.txt", 20000, fresh_base())[0]
    results = []

    def twice():
        results.append(upload(httpx.Client(base_url=BASE, timeout=900), single))

    threads = [threading.Thread(target=twice) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    final = wait(c, results[0]["id"])
    same = results[0]["id"] == results[1]["id"]
    report(c, 8, "Double clic, un seul résultat", None, final["inserted"], 0 if same else 1,
           f"même import : {same}, attendu 20000")


def t_cancel(c):
    base, path, broken, final, elapsed = run_import(c, 9, "Annulation", 10000)
    page = c.get(f"/api/imports/{final['id']}/transactions", params={"limit": 50}).json()
    for item in page["items"]:
        c.patch(f"/api/transactions/{item['id']}", json={"description": "corrigée à la main"}).raise_for_status()
    result = c.post(f"/api/imports/{final['id']}/cancel").json()
    kept = c.get(f"/api/imports/{final['id']}/transactions", params={"limit": 200}).json()["items"]
    ok = result["rolled_back"] == 10000 - 50 and len(kept) == 50
    report(c, 9, "Annulation chirurgicale", None, result["rolled_back"], 0 if ok else 1,
           f"conservées {len(kept)}, attendues 50")


def t_export(c):
    for challenge, ext in ((1, "json"), (14, "json")):
        started = time.perf_counter()
        count = 0
        seen = set()
        net = 0
        with c.stream("GET", f"/api/export.{ext}") as response:
            for line in response.iter_lines():
                if not line:
                    continue
                row = json.loads(line)
                count += 1
                seen.add(row["id"])
                net += row["amount"] if row["type"] == "CREDIT" else -row["amount"]
        elapsed = (time.perf_counter() - started) * 1000
        screen = c.get("/api/balance").json()["balance"]
        ok = count == len(seen) and net == screen
        report(c, challenge, "Export express" if challenge == 1 else "Export après correction", elapsed, count,
               0 if ok else 1, f"doublons {count - len(seen)}, total export {net}, solde affiché {screen}")


def t_search(c):
    latencies = []
    for _ in range(500):
        low = random.randint(1, 450000)
        params = {"type": random.choice(["CREDIT", "DEBIT"]), "min_amount": low,
                  "max_amount": low + random.randint(1000, 50000), "q": random.choice(WORDS), "limit": 50}
        started = time.perf_counter()
        c.get("/api/transactions", params=params).raise_for_status()
        latencies.append((time.perf_counter() - started) * 1000)
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    report(c, 3, "Recherche sous 200 ms (p95)", p95, 500, 0, f"p50 {latencies[250]:.0f} ms, max {latencies[-1]:.0f} ms")


def t_balance(c):
    latencies = []
    for _ in range(30):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - random.randint(1, 300) * 86400))
        started = time.perf_counter()
        c.get("/api/balance", params={"at": day}).raise_for_status()
        latencies.append((time.perf_counter() - started) * 1000)
    report(c, 11, "Le solde en un instant (max)", max(latencies), 30, 0, f"moyenne {sum(latencies) / 30:.1f} ms")


def t_memory(c):
    path, _ = make_file("million.txt", 1000000, fresh_base())
    samples = []
    stop = threading.Event()

    def poll():
        watcher = httpx.Client(base_url=BASE, timeout=30)
        t0 = time.perf_counter()
        while not stop.is_set():
            samples.append((round(time.perf_counter() - t0, 1), watcher.get("/api/health").json()["rss_mb"]))
            time.sleep(0.5)

    thread = threading.Thread(target=poll)
    thread.start()
    started = time.perf_counter()
    data = upload(c, path)
    final = wait(c, data["id"])
    elapsed = (time.perf_counter() - started) * 1000
    stop.set()
    thread.join()
    with open(os.path.join(WORK, "memoire.csv"), "w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["secondes", "rss_mo"])
        writer.writerows(samples)
    peak = max(s[1] for s in samples)
    report(c, 13, "Fichier sans fin (1 000 000 lignes)", elapsed, final["inserted"], final["errors"],
           f"pic mémoire {peak} Mo (trace dans {WORK}/memoire.csv)")


TASKS = {"import": t_import, "errors": t_errors, "parallel": t_parallel, "cancel": t_cancel,
         "export": t_export, "search": t_search, "balance": t_balance, "memory": t_memory}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", choices=list(TASKS) + ["all"])
    args = parser.parse_args()
    c = client()
    names = list(TASKS) if args.task == "all" else [args.task]
    for name in names:
        TASKS[name](c)


if __name__ == "__main__":
    sys.exit(main())
