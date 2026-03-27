"""Toy train script for E2E testing. Predicts next number in 1..100 sequence.
Prints val_bpb: line that the orchestrator parses. Runs in <1s on CPU."""

import random
import sys
import os
import time

start = time.time()

if os.environ.get("SWARM_E2E_FAKE_TRAIN") == "1":
    val_bpb = round(random.uniform(0.1, 1.5), 6)
    time.sleep(0.2)
else:
    data = list(range(1, 101))
    total_error = 0.0
    for i in range(len(data) - 1):
        prediction = data[i] + 1
        total_error += abs(prediction - data[i + 1])
    mse = total_error / (len(data) - 1)
    val_bpb = round(random.uniform(0.3, 1.2) + mse * 0.01, 6)
    time.sleep(0.5)

elapsed = time.time() - start

print("---")
print(f"val_bpb:          {val_bpb}")
print(f"training_seconds: {elapsed:.1f}")
print(f"total_seconds:    {elapsed:.1f}")
print(f"peak_vram_mb:     0.0")
sys.exit(0)
