import json
import secrets
import urllib.request
import urllib.error

def load_env():
    env = {}
    with open('.env') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            env[k] = v.strip().strip('"')
    return env

SERVICE_URL = "https://calendly-briefing-system-160101237293.us-central1.run.app"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

env = load_env()
token = env["CALENDLY_API_TOKEN"]

req = urllib.request.Request(
    "https://api.calendly.com/users/me",
    headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        me = json.loads(resp.read())["resource"]
except urllib.error.HTTPError as e:
    print("GET /users/me FAILED:", e.code, e.read().decode())
    raise SystemExit(1)

org_uri = me["current_organization"]
print("Organization:", org_uri)

signing_key = secrets.token_hex(32)

payload = json.dumps({
    "url": f"{SERVICE_URL}/webhooks/calendly",
    "events": ["invitee.created", "invitee.canceled"],
    "organization": org_uri,
    "scope": "organization",
    "signing_key": signing_key,
}).encode()

req = urllib.request.Request(
    "https://api.calendly.com/webhook_subscriptions",
    data=payload,
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": UA,
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    print("Webhook subscription created:", result["resource"]["uri"])

    with open(".env") as f:
        lines = f.readlines()
    with open(".env", "w") as f:
        found = False
        for line in lines:
            if line.startswith("CALENDLY_SIGNING_SECRET="):
                f.write(f"CALENDLY_SIGNING_SECRET={signing_key}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"CALENDLY_SIGNING_SECRET={signing_key}\n")
    print("Saved CALENDLY_SIGNING_SECRET into .env")
except urllib.error.HTTPError as e:
    print("Webhook creation FAILED:", e.code, e.read().decode())
