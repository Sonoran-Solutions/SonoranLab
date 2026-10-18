"""Build-time check (env/Containerfile): fail if the pip environment differs from the lock or the ROCm stack moved."""

import re
import subprocess
import sys

got = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=True).stdout.split()
want = open("/opt/sonoranlab/pip-freeze.p0.txt").read().split()
base = open("/opt/constraints.txt").read().split()
pin = re.compile(r"^(torch|torchvision|torchaudio|rocm|triton|pytorch-triton|numpy|opencv-python-headless)[=@ ]", re.I)
drift = sorted(set(want) ^ set(got)) + sorted(set(base) ^ {l for l in got if pin.match(l)})
if drift:
    sys.exit(f"pip environment drifted from env/pip-freeze.p0.txt or the base ROCm stack: {drift}")
open("/opt/sonoranlab-pip-freeze.txt", "w").write("\n".join(got) + "\n")
print(f"lock check ok: {len(got)} packages match env/pip-freeze.p0.txt; ROCm stack unchanged")
