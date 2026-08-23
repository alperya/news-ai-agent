#!/usr/bin/env python3
"""
Exchange a short-lived Instagram/Facebook user token for a long-lived one (60 days),
then update .env and AWS Secrets Manager.

Usage:
  python scripts/get_new_instagram_token.py

How to get the short-lived token:
  1. Go to https://developers.facebook.com/tools/explorer/
  2. Select your app (APP_ID: check .env)
  3. Click "Generate Access Token"
  4. Select permissions (select ALL of these so no feature loses access):
       ✅ instagram_basic
       ✅ instagram_content_publish
       ✅ instagram_manage_insights
       ✅ instagram_manage_comments
       ✅ pages_read_engagement
       ✅ pages_show_list
       ✅ pages_manage_posts      ← required for Facebook Page Stories
       ✅ business_management
  5. Authorize — copy the token that appears in the top field
  6. Paste it here when prompted
"""

import json
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# ── Load .env ──────────────────────────────────────────────────────────────────
env_vars: dict[str, str] = {}
with open(ENV_FILE) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env_vars[k] = v.strip('"').strip("'")

META_APP_ID = env_vars.get("META_APP_ID", "")
META_APP_SECRET = env_vars.get("META_APP_SECRET", "")

if not META_APP_ID or not META_APP_SECRET or META_APP_ID.startswith("your_"):
    print("❌ META_APP_ID or META_APP_SECRET not set in .env")
    sys.exit(1)

print(f"\n📱 Meta App ID: {META_APP_ID}")
print(
    "\n1. Visit: https://developers.facebook.com/tools/explorer/"
    f"\n2. Select app with ID: {META_APP_ID}"
    "\n3. Click 'Generate Access Token'"
    "\n4. Required permissions (select ALL — missing ones disable features):"
    "\n     ✅ instagram_basic"
    "\n     ✅ instagram_content_publish"
    "\n     ✅ instagram_manage_insights"
    "\n     ✅ instagram_manage_comments"
    "\n     ✅ pages_read_engagement"
    "\n     ✅ pages_show_list"
    "\n     ✅ pages_manage_posts      (required for Facebook Page Stories)"
    "\n     ✅ business_management"
    "\n5. Authorize and copy the token from the top field\n"
)

short_token = input("Paste the short-lived token here: ").strip()
if not short_token:
    print("❌ No token provided.")
    sys.exit(1)

# ── Exchange for long-lived token ─────────────────────────────────────────────
print("\n🔄 Exchanging for long-lived token (60 days)...")
params = urllib.parse.urlencode({
    "grant_type": "fb_exchange_token",
    "client_id": META_APP_ID,
    "client_secret": META_APP_SECRET,
    "fb_exchange_token": short_token,
})
url = f"https://graph.facebook.com/oauth/access_token?{params}"

try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())
except Exception as e:
    print(f"❌ Exchange request failed: {e}")
    sys.exit(1)

if "error" in data:
    print(f"❌ Meta API error: {data['error']['message']}")
    sys.exit(1)

new_token = data["access_token"]
expires_in = data.get("expires_in", 5183944)  # seconds
expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
print(f"✅ New token obtained — expires {expiry.strftime('%Y-%m-%d')} UTC")

# ── Update .env ───────────────────────────────────────────────────────────────
env_text = ENV_FILE.read_text()
old_line = next(
    (line for line in env_text.splitlines() if line.startswith("INSTAGRAM_ACCESS_TOKEN=")),
    None,
)
if old_line:
    env_text = env_text.replace(old_line, f"INSTAGRAM_ACCESS_TOKEN={new_token}")
    ENV_FILE.write_text(env_text)
    print("✅ .env updated")
else:
    print("⚠️  Could not find INSTAGRAM_ACCESS_TOKEN in .env — update it manually")

# ── Update Secrets Manager ────────────────────────────────────────────────────
try:
    import boto3

    secret_name = env_vars.get("SECRET_NAME", "news-ai-agent/credentials")
    region = env_vars.get("AWS_REGION", "eu-central-1")
    client = boto3.client("secretsmanager", region_name=region)

    existing = json.loads(client.get_secret_value(SecretId=secret_name)["SecretString"])
    existing["INSTAGRAM_ACCESS_TOKEN"] = new_token
    client.put_secret_value(
        SecretId=secret_name,
        SecretString=json.dumps(existing, ensure_ascii=False),
    )
    print(f"✅ Secrets Manager updated ({secret_name})")
except ImportError:
    print("⚠️  boto3 not available — Secrets Manager not updated (run from project venv)")
except Exception as e:
    print(f"⚠️  Secrets Manager update failed: {e}")
    print("   Run: python scripts/update_secrets.py  (after .env is saved)")

print(f"\n🎉 Done! New token valid until {expiry.strftime('%Y-%m-%d')}.")
print("   The auto-refresh Lambda will keep it alive going forward.")
