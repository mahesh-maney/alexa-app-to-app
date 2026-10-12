#!/usr/bin/env bash
# update_cognito_logo.sh — Upload a logo to Cognito Managed Login branding
#
# Usage:
#   ./update_cognito_logo.sh /path/to/logo.png
#
# Requirements:
#   - AWS CLI configured with credentials that have cognito-idp permissions
#   - Python 3 with Pillow  (pip install Pillow)
#
# Cognito config (edit these if deploying elsewhere):
USER_POOL_ID="ap-south-1_h1o8s7257"
BRANDING_ID="eb333a03-5140-491e-b2b3-72efb3cb976e"
REGION="ap-south-1"

set -euo pipefail

# -- 1. Validate input ---------------------------------------------------------
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/logo.png"
  exit 1
fi

INPUT_LOGO="$1"
if [[ ! -f "$INPUT_LOGO" ]]; then
  echo "Error: file not found: $INPUT_LOGO"
  exit 1
fi

echo "-> Input logo: $INPUT_LOGO"

# -- 2. Pad logo to meet Cognito's 4:1 max width:height ratio ------------------
PADDED_LOGO=$(mktemp /tmp/cognito_logo_padded_XXXXXX.png)
trap 'rm -f "$PADDED_LOGO"' EXIT

python3.9 - "$INPUT_LOGO" "$PADDED_LOGO" <<'PYEOF'
import sys
from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
img = Image.open(src).convert("RGBA")
w, h = img.size

# Cognito requires width:height ratio <= 4:1
min_h = (w + 3) // 4  # ceiling so that w/h <= 4
if h < min_h:
    print(f"  Padding height {h}px -> {min_h}px to meet 4:1 ratio")
    canvas = Image.new("RGBA", (w, min_h), (0, 0, 0, 0))
    canvas.paste(img, (0, (min_h - h) // 2))
    canvas.save(dst, "PNG")
else:
    print(f"  Logo {w}x{h} already within 4:1 ratio, no padding needed")
    img.save(dst, "PNG")
PYEOF

SIZE=$(du -sh "$PADDED_LOGO" | cut -f1)
echo "-> Prepared logo: $SIZE"

# -- 3. Fetch current merged branding settings ---------------------------------
echo "-> Fetching current branding settings..."
SETTINGS=$(aws cognito-idp describe-managed-login-branding \
  --region "$REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --managed-login-branding-id "$BRANDING_ID" \
  --return-merged-resources \
  --query 'ManagedLoginBranding.Settings' \
  --output json)

# -- 4. Enable logo on all components that have one ---------------------------
SETTINGS=$(python3.9 - "$SETTINGS" <<'PYEOF'
import sys, json
settings = json.loads(sys.argv[1]) if sys.argv[1] != "null" else {}
for k, v in settings.get("components", {}).items():
    if isinstance(v, dict) and "logo" in v:
        was = v["logo"].get("enabled", False)
        settings["components"][k]["logo"]["enabled"] = True
        print(f"  components.{k}.logo.enabled: {was} -> True", file=sys.stderr)
print(json.dumps(settings))
PYEOF
)

# -- 5. Encode logo as base64 --------------------------------------------------
LOGO_B64=$(base64 -i "$PADDED_LOGO" | tr -d '\n')

ASSETS=$(python3.9 -c "
import json, sys
b = open('$PADDED_LOGO', 'rb').read()
import base64
b64 = base64.b64encode(b).decode()
assets = []
for cat in ['FORM_LOGO', 'PAGE_HEADER_LOGO']:
    for mode in ['LIGHT', 'DARK']:
        assets.append({'Category': cat, 'ColorMode': mode, 'Extension': 'PNG', 'Bytes': b64})
print(json.dumps(assets))
")

# -- 6. Push to Cognito --------------------------------------------------------
echo "-> Uploading to Cognito Managed Login branding..."
aws cognito-idp update-managed-login-branding \
  --region "$REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --managed-login-branding-id "$BRANDING_ID" \
  --settings "$SETTINGS" \
  --assets "$ASSETS" \
  --query 'ManagedLoginBranding.ManagedLoginBrandingId' \
  --output text

echo "Done. Login page: https://auth.digilux.co.in/login"
