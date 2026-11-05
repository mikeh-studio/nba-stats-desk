"""Bounded single-flight cache; failed refreshes never serve stale controls."""

import copy
import threading
import time
from collections import OrderedDict


class ControlCache:
    def __init__(self, ttl, *, limit=8, timer=time.monotonic):
        if not 0 <= ttl <= 60 or limit < 1:
            raise ValueError("invalid control cache bounds")
        self.ttl, self.limit, self.timer = ttl, limit, timer
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key, load):
        if not self.ttl:
            return load()
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None and self.timer() < entry[0]:
                self.entries.move_to_end(key)
                return copy.deepcopy(entry[1])
            self.entries.pop(key, None)
            # Timestamp before loading: a slow remote call consumes its lease.
            deadline = self.timer() + self.ttl
            value = load()
            self.entries[key] = deadline, copy.deepcopy(value)
            while len(self.entries) > self.limit:
                self.entries.popitem(last=False)
            return value
