#!/usr/bin/env python3
import base64

data = open('C:/Users/XIAOMI/WorkBuddy/2026-09-25-15-54-18/v110.py', 'rb').read()
lines = []
for i in range(0, len(data), 2800):
    chunk = data[i:i+2800]
    b64 = base64.b64encode(chunk).decode()
    # Use echo with single quotes, escape single quotes in b64 (shouldn't have any)
    lines.append(f"echo '{b64}' >> /tmp/b64_chunk.txt")

lines.append("base64 -d < /tmp/b64_chunk.txt > /tmp/v110.py && echo DECODED")
lines.append("docker cp /tmp/v110.py moviepilot-v3:/tmp/v110.py && echo CP_OK")
lines.append('docker exec -u 1000:10 moviepilot-v3 sh -c "cp /tmp/v110.py /app/app/plugins/embyunwatchedwash/__init__.py && rm -rf /app/app/plugins/embyunwatchedwash/__pycache__ && chmod 777 /app/app/plugins/embyunwatchedwash/__init__.py && echo COPIED"')
lines.append("docker exec -u 1000:10 moviepilot-v3 python3 -m py_compile /app/app/plugins/embyunwatchedwash/__init__.py && echo COMPILE_OK")
lines.append('curl -s -m 30 -X POST "http://127.0.0.1:11317/api/v1/plugin/reload/embyunwatchedwash?apikey=yqD6sgGxuWUs7WtMPokMMQ"')
lines.append("rm -f /tmp/b64_chunk.txt /tmp/v110.py")

with open('C:/Users/XIAOMI/WorkBuddy/2026-09-25-15-54-18/deploy_v110.sh', 'w') as f:
    f.write('\n'.join(lines) + '\n')
print(f'written {len(lines)} lines')
