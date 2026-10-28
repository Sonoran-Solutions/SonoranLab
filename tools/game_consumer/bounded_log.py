"""Drain a child's combined output into bounded files: the first HEAD bytes, a rolling TAIL-byte tail, and the total
produced. Past MAX produced bytes it saves what it has, writes PREFIX.flood and stops reading for a while, so the
writer blocks (output stops) until the supervisor kills the job; then it drains to EOF without storing.
Usage: bounded_log.py PREFIX [HEAD TAIL MAX] < output"""

import sys
import time
from collections import deque

prefix = sys.argv[1]
head_max, tail_max, flood_max = (int(a) for a in (sys.argv[2:5] or (1 << 20, 4 << 20, 64 << 20)))
head, tail, tail_len, total = bytearray(), deque(), 0, 0


def save() -> None:
    with open(prefix + ".head.log", "wb") as f:
        f.write(head)
    with open(prefix + ".tail.log", "wb") as f:
        f.write(b"".join(tail)[-tail_max:])
    with open(prefix + ".bytes", "w") as f:
        f.write(f"{total}\n")


src = sys.stdin.buffer
while chunk := src.read1(65536):
    total += len(chunk)
    if len(head) < head_max:
        take = chunk[: head_max - len(head)]
        head += take
        chunk = chunk[len(take) :]
    if chunk:
        tail.append(chunk)
        tail_len += len(chunk)
        while tail_len - len(tail[0]) >= tail_max:
            tail_len -= len(tail.popleft())
    if total > flood_max:
        save()
        with open(prefix + ".flood", "w") as f:
            f.write(f"{total}\n")
        time.sleep(15)  # the writer blocks on the full pipe; the supervisor kills its group meanwhile
        while src.read1(65536):
            pass
        sys.exit(0)
save()
