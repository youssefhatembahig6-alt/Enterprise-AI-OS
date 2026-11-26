import base64, json, sys, time, urllib.request
API = "http://localhost:18000"
C = json.load(open(sys.argv[1]))["niletech"]
def call(m, p, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    r = urllib.request.Request(API + p, data=json.dumps(body).encode() if body is not None else None, headers=h, method=m)
    with urllib.request.urlopen(r, timeout=60) as x:
        return x.status, json.loads(x.read() or b"null")
tok = call("POST", "/api/v1/auth/login", body={"tenant": "niletech", "email": "access.admin@niletech.example", "password": C["access.admin@niletech.example"]})[1]["access_token"]
memo = open("backend/fixtures/m7-acceptance/sales-territory-memo.md", "rb").read()
mixed_total = 0
for n in range(1, 9):
    body = {"filename": f"m7-b2-probe-{n}.md", "content_base64": base64.b64encode(memo + f"\nProbe {n}.\n".encode()).decode(),
            "department": "sales", "permissions": {"classification": "internal", "grants": [{"group": "all-staff"}]}}
    st, res = call("POST", "/api/v1/documents", tok, body)
    doc, t0, seen = res["document_id"], time.monotonic(), []
    while time.monotonic() - t0 < 20:
        d = next((x for x in call("GET", "/api/v1/documents", tok)[1] if x["document_id"] == doc), None)
        s = (d["status"], d["searchable"]) if d else ("absent", None)
        if not seen or seen[-1][1] != s: seen.append((round(time.monotonic() - t0, 3), s))
        if s == ("ready", True): break
        time.sleep(0.02)
    mixed = [x for x in seen if x[1] == ("ready", False)]
    mixed_total += len(mixed)
    print(f"probe {n}: post={st} transitions={seen} ready_but_unsearchable={len(mixed)}")
print("TOTAL ready_but_unsearchable observations:", mixed_total)
