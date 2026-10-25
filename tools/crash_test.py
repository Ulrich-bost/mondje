import subprocess
import sys
import time

import httpx

import bench

LINES = 200000
KILL_AT = 60000


def compose(*args):
    return subprocess.run(["sudo", "-n", "docker", "compose", *args], capture_output=True, text=True)


def api_up(client):
    try:
        return client.get("/api/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def main():
    client = bench.client()
    path, _ = bench.make_file("crash.txt", LINES, bench.fresh_base())
    started = time.perf_counter()
    import_id = bench.upload(client, path)["id"]
    print("import lancé :", import_id, flush=True)

    last = 0
    while True:
        data = client.get(f"/api/imports/{import_id}").json()
        last = data["lines_read"]
        if data["status"] != "running":
            print("l'import s'est terminé avant la coupure, test impossible", flush=True)
            sys.exit(1)
        if last >= KILL_AT:
            break
        time.sleep(0.05)

    print(f"coupure brutale de l'API à {last} lignes lues", flush=True)
    result = compose("kill", "-s", "KILL", "api")
    if result.returncode:
        print(result.stderr)
        print("lance d'abord : sudo -v", flush=True)
        sys.exit(1)
    time.sleep(1)
    print("API arrêtée :", not api_up(client), flush=True)

    compose("up", "-d", "api")
    for _ in range(120):
        if api_up(client):
            break
        time.sleep(0.5)
    else:
        print("l'API ne redémarre pas", flush=True)
        sys.exit(1)

    after = client.get(f"/api/imports/{import_id}").json()
    print(f"après redémarrage : {after['lines_read']} lignes lues (avant la coupure : {last})", flush=True)

    while True:
        final = client.get(f"/api/imports/{import_id}").json()
        if final["status"] != "running":
            break
        time.sleep(0.5)
    elapsed = (time.perf_counter() - started) * 1000

    resumed = after["lines_read"] >= last
    complete = final["status"] == "done" and final["inserted"] + final["duplicates"] == LINES and final["errors"] == 0
    print(f"statut {final['status']}, {final['inserted']} insérées, {final['duplicates']} doublons, {final['errors']} erreurs", flush=True)
    print("reprise sans relecture depuis le début :", resumed, flush=True)
    print("import complet sans doublon :", complete, flush=True)
    bench.report(
        client, 12, "Import coupé en plein milieu (200 000 lignes)", elapsed,
        final["inserted"], final["errors"],
        f"coupure à {last} lignes, reprise à {after['lines_read']}, statut {final['status']}",
    )


main()
