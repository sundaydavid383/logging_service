import jwt
import time
from pathlib import Path

private_key = Path("keys/private.pem").read_text()
now = int(time.time())

token = jwt.encode(
    {
        "sub": "00000000-0000-0000-0000-000000000001",
        "iat": now,
        "exp": now + 3600,
        "scope": "logs:write logs:read audit:append audit:read audit:verify notifications:configure notifications:read notifications:send",
        "role": "system_admin",
    },
    private_key,
    algorithm="RS256",
)

print(token)