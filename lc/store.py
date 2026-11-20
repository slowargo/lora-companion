"""SQLite store: one row per LoRA file content (fingerprint), one row per event.

Counts are SUM(delta) per (lora, kind), kept in memory and updated on every write.
The path is only the last place the file was seen, so renaming or moving a LoRA keeps its stats.
"""
import logging
import os
import sqlite3
import threading
import time

import folder_paths

from .fingerprint import fingerprint

LOG = "[lora-companion]"
KINDS = ("use", "like", "dislike")

SCHEMA = """
CREATE TABLE IF NOT EXISTS loras (
  id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL UNIQUE,
  path TEXT,
  size INTEGER,
  mtime_ns INTEGER,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  lora_id INTEGER NOT NULL REFERENCES loras(id),
  kind TEXT NOT NULL,
  delta INTEGER NOT NULL,
  strength REAL,
  strength_clip REAL,
  prompt_id TEXT,
  source TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS events_lora_kind ON events(lora_id, kind);
CREATE INDEX IF NOT EXISTS events_prompt ON events(prompt_id);
"""


class Store:

  def __init__(self, db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    self.lock = threading.RLock()
    self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    self.db.execute("PRAGMA journal_mode=WAL")
    self.db.execute("PRAGMA synchronous=NORMAL")
    self.db.executescript(SCHEMA)
    # relname -> (size, mtime_ns, lora_id)
    self.path_cache = {}
    # lora_id -> {"use": n, "like": n, "dislike": n, "last_used": ts}
    self.totals = {}
    self.last_prompt_id = None
    for lid, kind, total in self.db.execute(
        "SELECT lora_id, kind, SUM(delta) FROM events GROUP BY lora_id, kind"):
      self._totals(lid)[kind] = total
    for lid, ts in self.db.execute(
        "SELECT lora_id, MAX(ts) FROM events WHERE kind='use' GROUP BY lora_id"):
      self._totals(lid)["last_used"] = ts
    for lid, path, size, mtime_ns in self.db.execute(
        "SELECT id, path, size, mtime_ns FROM loras WHERE path IS NOT NULL"):
      self.path_cache[path] = (size, mtime_ns, lid)

  def _totals(self, lid):
    t = self.totals.get(lid)
    if t is None:
      t = self.totals[lid] = {"use": 0, "like": 0, "dislike": 0, "last_used": None}
    return t

  # --- identity ---

  def resolve(self, relname, compute=True):
    """lora_id for a file in the loras folder, fingerprinting it when new or changed."""
    full = folder_paths.get_full_path("loras", relname)
    if full is None:
      return None
    try:
      st = os.stat(full)
    except OSError:
      return None
    with self.lock:
      hit = self.path_cache.get(relname)
      if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    if not compute:
      return None
    try:
      fp = fingerprint(full)
    except OSError as e:
      logging.warning("%s cannot read %s: %s", LOG, full, e)
      return None
    now = time.time()
    with self.lock:
      row = self.db.execute("SELECT id, path FROM loras WHERE fingerprint=?", (fp,)).fetchone()
      if row:
        lid, old_path = row
        self.db.execute("UPDATE loras SET path=?, size=?, mtime_ns=?, last_seen=? WHERE id=?",
                        (relname, st.st_size, st.st_mtime_ns, now, lid))
        # Identical copies under two names share one row; only a vanished old path is a rename.
        if old_path and old_path != relname and folder_paths.get_full_path("loras", old_path) is None:
          self.path_cache.pop(old_path, None)
          if lid in self.totals:
            logging.info("%s %s is now %s", LOG, old_path, relname)
      else:
        lid = self.db.execute(
          "INSERT INTO loras(fingerprint, path, size, mtime_ns, first_seen, last_seen) VALUES(?,?,?,?,?,?)",
          (fp, relname, st.st_size, st.st_mtime_ns, now, now)).lastrowid
      # Another row may still claim this path (file replaced by a different one): unlink it.
      self.db.execute("UPDATE loras SET path=NULL WHERE path=? AND id<>?", (relname, lid))
      self.path_cache[relname] = (st.st_size, st.st_mtime_ns, lid)
      return lid

  def warm(self):
    """Fingerprint every LoRA file not seen yet (run in a background thread)."""
    t0 = time.perf_counter()
    n = 0
    for relname in folder_paths.get_filename_list("loras"):
      if self.resolve(relname, compute=False) is None:
        if self.resolve(relname) is not None:
          n += 1
    if n:
      logging.info("%s fingerprinted %d LoRA files in %.1fs", LOG, n, time.perf_counter() - t0)

  # --- reads ---

  def stats_for_id(self, lid):
    t = self.totals.get(lid)
    if not t:
      return {"uses": 0, "likes": 0, "dislikes": 0, "last_used": None}
    return {"uses": t["use"], "likes": t["like"], "dislikes": t["dislike"], "last_used": t["last_used"]}

  def stats(self, relname):
    lid = self.resolve(relname)
    return None if lid is None else self.stats_for_id(lid)

  def all_stats(self):
    """relname -> stats for every current LoRA file that has any events."""
    out = {}
    for relname in folder_paths.get_filename_list("loras"):
      lid = self.resolve(relname)
      if lid is not None and lid in self.totals:
        out[relname] = self.stats_for_id(lid)
    return out

  # --- writes ---

  def record_uses(self, uses, prompt_id):
    """uses: {relname: (strength, strength_clip)}. Returns {relname: stats} of the changed LoRAs."""
    now = time.time()
    changed = {}
    rows = []
    for relname, (strength, strength_clip) in uses.items():
      lid = self.resolve(relname)
      if lid is None:
        continue
      # Identical copies under two names are one LoRA: count it once, but push both names.
      if lid not in changed.values():
        rows.append((now, lid, "use", 1, strength, strength_clip, prompt_id, "live"))
      changed[relname] = lid
    with self.lock:
      if rows:
        self.db.executemany(
          "INSERT INTO events(ts, lora_id, kind, delta, strength, strength_clip, prompt_id, source)"
          " VALUES(?,?,?,?,?,?,?,?)", rows)
        for _, lid, *_ in rows:
          t = self._totals(lid)
          t["use"] += 1
          t["last_used"] = now
      self.last_prompt_id = prompt_id
      return {r: self.stats_for_id(lid) for r, lid in changed.items()}

  def first_live_ts(self):
    with self.lock:
      row = self.db.execute("SELECT MIN(ts) FROM events WHERE kind='use' AND source='live'").fetchone()
    return row[0] if row else None

  def add_backfill(self, plan):
    """plan: [(ts, prompt_id, {relname: (strength, strength_clip)})]. Skips prompt_ids already stored."""
    inserted = 0
    for ts, prompt_id, uses in plan:
      lids = {}
      for relname, (s, s2) in uses.items():
        lid = self.resolve(relname)
        if lid is not None and lid not in lids:
          lids[lid] = (s, s2)
      if not lids:
        continue
      with self.lock:
        if self.db.execute("SELECT 1 FROM events WHERE prompt_id=? LIMIT 1", (prompt_id,)).fetchone():
          continue
        self.db.executemany(
          "INSERT INTO events(ts, lora_id, kind, delta, strength, strength_clip, prompt_id, source)"
          " VALUES(?,?,?,?,?,?,?,?)",
          [(ts, lid, "use", 1, s, s2, prompt_id, "backfill") for lid, (s, s2) in lids.items()])
        for lid in lids:
          t = self._totals(lid)
          t["use"] += 1
          if t["last_used"] is None or ts > t["last_used"]:
            t["last_used"] = ts
        inserted += 1
    return inserted

  def vote(self, relname, kind, delta):
    if kind not in ("like", "dislike") or delta not in (1, -1):
      raise ValueError("bad vote")
    lid = self.resolve(relname)
    if lid is None:
      return None
    with self.lock:
      t = self._totals(lid)
      if delta < 0 and t[kind] <= 0:
        return self.stats_for_id(lid)
      self.db.execute(
        "INSERT INTO events(ts, lora_id, kind, delta, prompt_id, source) VALUES(?,?,?,?,?,?)",
        (time.time(), lid, kind, delta, self.last_prompt_id, "live"))
      t[kind] += delta
      return self.stats_for_id(lid)
