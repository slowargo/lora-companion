"""Civitai LoRA downloader.

Resolves a Civitai model page URL to its version file, downloads it into a subfolder of the
configured loras root, then has rgthree build the .rgthree-info.json (its own Civitai fetch) and
stores the user's note as the info's Additional Notes. The job queue survives restarts; an
interrupted download resumes from its .part file.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

import requests
import folder_paths
from comfy.cli_args import args
from server import PromptServer

LOG = "[lora-companion]"
DATA_DIR = os.path.join(folder_paths.get_user_directory(), "lora-companion")
CONFIG_FILE = os.path.join(DATA_DIR, "downloader.json")
AUTH_FILE = os.path.join(DATA_DIR, "civitai_auth.txt")
JOBS_FILE = os.path.join(DATA_DIR, "downloads.json")
DEFAULT_CONFIG = {"root": "", "exclude": [], "title_strip": []}
ACTIVE = ("queued", "downloading", "info")
CHUNK = 1 << 20


class Canceled(Exception):
  pass


def _read_json(path, default):
  try:
    with open(path, encoding="utf-8") as f:
      return json.load(f)
  except FileNotFoundError:
    return default
  except ValueError:
    logging.warning("%s %s is corrupt, moved to %s.bad", LOG, path, path)
    os.replace(path, path + ".bad")
    return default


def _write_json(path, data):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
    f.flush()
    os.fsync(f.fileno())
  os.replace(tmp, path)


def load_config():
  return {**DEFAULT_CONFIG, **_read_json(CONFIG_FILE, {})}


def save_config(body):
  cfg = load_config()
  if "root" in body:
    cfg["root"] = str(body["root"]).strip().strip("/\\")
    root_dir(cfg)
  for key in ("exclude", "title_strip"):
    if key in body:
      cfg[key] = [str(v).strip() for v in body[key] if str(v).strip()]
  _write_json(CONFIG_FILE, cfg)
  if "auth" in body:
    auth = str(body["auth"]).strip()
    if auth:
      os.makedirs(DATA_DIR, exist_ok=True)
      with open(AUTH_FILE, "w", encoding="utf-8") as f:
        f.write(auth)
    elif os.path.exists(AUTH_FILE):
      os.remove(AUTH_FILE)
  return public_config()


def _auth():
  try:
    with open(AUTH_FILE, encoding="utf-8") as f:
      return f.read().strip()
  except FileNotFoundError:
    return ""


def loras_dir():
  return os.path.abspath(folder_paths.get_folder_paths("loras")[0])


def root_dir(cfg):
  base = loras_dir()
  root = os.path.abspath(os.path.join(base, cfg["root"]))
  if os.path.commonpath([base, root]) != base:
    raise ValueError("root must be inside the loras folder")
  return root


def subdirs(cfg):
  root = root_dir(cfg)
  if not os.path.isdir(root):
    return []
  exclude = {e.lower() for e in cfg["exclude"]}
  names = sorted(e.name for e in os.scandir(root) if e.is_dir() and e.name.lower() not in exclude)
  return names or [""]


def public_config():
  cfg = load_config()
  auth = _auth()
  return {**cfg, "loras_dir": loras_dir(), "subdirs": subdirs(cfg),
          "auth": "cookie" if "=" in auth else ("key" if auth else "")}


# ---- naming


_SEP = r"[\s_.\-]*"


def clean_title(title, words):
  """Drop base-model tags such as 'Krea2' / '[KR2]' / 'for Krea 2' from a model title."""
  out = title
  for w in words:
    parts = [re.escape(p) for p in re.split(r"[\s_.\-]+", w.strip()) if p]
    if not parts:
      continue
    core = r"(?<![^\W_])" + _SEP.join(parts) + r"(?![^\W_])"
    out = re.sub(r"[(\[{]\s*" + core + r"\s*[)\]}]", " ", out, flags=re.I)
    out = re.sub(r"(?:\bfor\s+)?" + core, " ", out, flags=re.I)
  out = re.sub(r"[(\[{]\s*[)\]}]", " ", out)
  out = re.sub(r"(^|\s)[_.]+", r"\1", out)
  out = re.sub(r"\s*([-|/:,·–—])(?:\s*[-|/:,·–—])+", r" \1", out)
  out = re.sub(r"\s+", " ", out).strip(" -|/:,._·–—")
  return out or title.strip()


def sanitize(name):
  # Commas too: rgthree's info routes take comma-separated file lists.
  name = re.sub(r'[\\/:*?"<>|,\x00-\x1f]', " ", name)
  return re.sub(r"\s+", " ", name).strip(" .")


def _norm(s):
  return re.sub(r"[\W_]+", "", s).lower()


def default_stem(title, file_name):
  stem = os.path.splitext(file_name)[0]
  if title and not _norm(stem).startswith(_norm(title)):
    stem = f"{title} {stem}"
  return sanitize(stem)


# ---- civitai


def _session(host):
  s = requests.Session()
  s.headers["User-Agent"] = "lora-companion"
  auth = _auth()
  if "=" in auth:
    for part in auth.split(";"):
      k, _, v = part.strip().partition("=")
      if k:
        s.cookies.set(k, v, domain=host)
  elif auth:
    # requests drops Authorization when the download redirects to the storage host.
    s.headers["Authorization"] = f"Bearer {auth}"
  return s


def parse_url(url):
  u = urlparse(url.strip())
  host = (u.hostname or "").lower()
  if host.startswith("www."):
    host = host[4:]
  if host not in ("civitai.com", "civitai.red"):
    raise ValueError("not a civitai.com / civitai.red URL")
  version = parse_qs(u.query).get("modelVersionId", [None])[0]
  model = None
  m = re.search(r"/(api/download/)?models/(\d+)", u.path)
  if m and m.group(1):
    version = m.group(2)
  elif m:
    model = m.group(2)
  if not version and not model:
    raise ValueError("no model or version id in the URL")
  return host, model, version


def _get_json(s, url):
  r = s.get(url, timeout=30)
  if r.status_code == 404:
    raise ValueError("not found on Civitai")
  r.raise_for_status()
  return r.json()


def _find_existing(sha256):
  sha256 = sha256.lower()
  for dirpath, _, files in os.walk(loras_dir()):
    for f in files:
      if f.endswith(".rgthree-info.json"):
        try:
          with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
            if sha256 in fh.read().lower():
              return os.path.relpath(os.path.join(dirpath, f[:-len(".rgthree-info.json")]), loras_dir())
        except OSError:
          pass
  return None


def resolve(url):
  host, model, version = parse_url(url)
  s = _session(host)
  if not version:
    versions = _get_json(s, f"https://{host}/api/v1/models/{model}").get("modelVersions") or []
    if not versions:
      raise ValueError("model has no versions")
    version = versions[0]["id"]
  v = _get_json(s, f"https://{host}/api/v1/model-versions/{version}")
  files = sorted((f for f in v.get("files") or [] if f.get("type") == "Model"),
                 key=lambda f: not f.get("primary"))
  if not files:
    raise ValueError("version has no model file")
  f = files[0]
  title = (v.get("model") or {}).get("name") or ""
  words = load_config()["title_strip"] + [w for w in [v.get("baseModel")] if w and w != "Other"]
  clean = clean_title(title, words)
  sha256 = ((f.get("hashes") or {}).get("SHA256") or "").lower()
  ext = os.path.splitext(f.get("name") or "")[1]
  download_url = f.get("downloadUrl") or v.get("downloadUrl") or ""
  if urlparse(download_url).hostname not in ("civitai.com", "civitai.red"):
    raise ValueError(f"unexpected download host in {download_url!r}")
  return {
    "host": host,
    "model_id": v.get("modelId"),
    "version_id": v.get("id"),
    "page_url": f"https://{host}/models/{v.get('modelId')}?modelVersionId={v.get('id')}",
    "title": title,
    "clean_title": clean,
    "version_name": v.get("name"),
    "base_model": v.get("baseModel"),
    "type": (v.get("model") or {}).get("type"),
    "file_name": f.get("name"),
    "size": int((f.get("sizeKB") or 0) * 1024),
    "sha256": sha256,
    "download_url": download_url,
    "stem": default_stem(clean, f.get("name") or ""),
    "ext": ext if re.fullmatch(r"\.[A-Za-z0-9]+", ext) else ".safetensors",
    "existing": _find_existing(sha256) if sha256 else None,
  }


# ---- jobs


def _local_base():
  host = (args.listen or "127.0.0.1").split(",")[0].strip()
  if host in ("", "0.0.0.0"):
    host = "127.0.0.1"
  elif host == "::":
    host = "::1"
  if ":" in host:
    host = f"[{host}]"
  scheme = "https" if args.tls_keyfile and args.tls_certfile else "http"
  return f"{scheme}://{host}:{PromptServer.instance.port}"


def _local_get(path, **kwargs):
  s = requests.Session()
  s.trust_env = False  # never route the call to this server through HTTP(S)_PROXY
  base = _local_base()
  return s.get(base + path, verify=not base.startswith("https"), timeout=600, **kwargs)


def _part(job):
  return f"{job['target']}.{job['id']}.part"


class Downloader:

  def __init__(self):
    self.lock = threading.Condition()
    self.jobs = _read_json(JOBS_FILE, [])
    for job in self.jobs:
      if job["status"] in ("downloading", "info"):
        job["status"] = "queued"
      job["done"] = job.get("done", 0)
      job["speed"] = 0
    self.cancel = set()
    self.thread = None

  def start(self):
    self.thread = threading.Thread(target=self._loop, name="lora-companion-dl", daemon=True)
    self.thread.start()

  def _save(self):
    # Memory is the source of truth; a failed write (file locked, disk full) is redone on the next change.
    try:
      _write_json(JOBS_FILE, [{k: v for k, v in j.items() if k != "speed"} for j in self.jobs])
    except OSError as e:
      logging.warning("%s could not save %s: %s", LOG, JOBS_FILE, e)

  def _get(self, job_id):
    for job in self.jobs:
      if job["id"] == job_id:
        return job
    raise KeyError(job_id)

  def _taken(self, target, job_id=None):
    key = os.path.normcase(target)
    return any(j["id"] != job_id and j["status"] != "done" and os.path.normcase(j["target"]) == key
               for j in self.jobs)

  def list(self):
    with self.lock:
      return [dict(j) for j in self.jobs]

  def submit(self, url, subdir, stem, note):
    info = resolve(url)
    cfg = load_config()
    if subdir not in subdirs(cfg):
      raise ValueError("unknown folder")
    stem = sanitize(stem or "")
    if not stem:
      raise ValueError("empty file name")
    target = os.path.join(root_dir(cfg), subdir, stem + info["ext"])
    if "," in os.path.relpath(target, loras_dir()):
      raise ValueError("the folder path contains a comma, which rgthree cannot handle")
    if os.path.exists(target):
      raise ValueError("file already exists")
    job = {**info, "id": uuid.uuid4().hex[:12], "subdir": subdir, "stem": stem, "note": note or "",
           "target": target, "status": "queued", "error": "", "warning": "", "done": 0, "speed": 0,
           "downloaded": False, "created": time.time(), "started": None, "finished": None}
    with self.lock:
      if self._taken(target):
        raise ValueError("another job in the list uses the same file name")
      self.jobs.append(job)
      self._save()
      self.lock.notify_all()
    return dict(job)

  def action(self, job_id, action):
    with self.lock:
      job = self._get(job_id)
      status = job["status"]
      if action == "cancel" and status == "queued":
        job["status"] = "canceled"
      elif action == "cancel" and status == "downloading":
        self.cancel.add(job_id)
      elif action == "retry" and status in ("failed", "canceled"):
        if self._taken(job["target"], job_id) or (not job["downloaded"] and os.path.exists(job["target"])):
          raise ValueError("the file name is now used by another job or file")
        job.update(status="queued", error="", warning="")
      elif action == "remove" and status not in ACTIVE:
        self.jobs.remove(job)
        if os.path.exists(_part(job)):
          os.remove(_part(job))
      else:
        raise ValueError(f"cannot {action} a {status} job")
      self._save()
      self.lock.notify_all()

  def clear_finished(self):
    with self.lock:
      self.jobs = [j for j in self.jobs if j["status"] != "done"]
      self._save()

  def _loop(self):
    while True:
      try:
        self._step()
      except Exception:
        logging.exception("%s downloader error", LOG)
        time.sleep(5)

  def _step(self):
    with self.lock:
      job = next((j for j in self.jobs if j["status"] == "queued"), None)
      if job is None:
        self.lock.wait()
        return
      job.update(status="info" if job["downloaded"] else "downloading", started=time.time(), finished=None)
      self.cancel.discard(job["id"])
      self._save()
    try:
      self._run(job)
      status, error = "done", ""
    except Exception as e:
      if job["id"] in self.cancel:
        if os.path.exists(_part(job)):
          os.remove(_part(job))
        status, error = "canceled", ""
      else:
        logging.warning("%s download %s failed: %s", LOG, job["stem"], e)
        status, error = "failed", str(e) or type(e).__name__
    with self.lock:
      self.cancel.discard(job["id"])
      job.update(status=status, error=error, speed=0, finished=time.time())
      self._save()

  def _run(self, job):
    target = job["target"]
    if not job["downloaded"]:
      self._download(job, target)
      with self.lock:
        job.update(downloaded=True, status="info", speed=0, done=os.path.getsize(target))
        self._save()
    self._info(job, target)

  def _download(self, job, target):
    if os.path.exists(target):
      raise ValueError("file already exists")
    part = _part(job)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    h = hashlib.sha256()
    start = 0
    if os.path.exists(part):
      with open(part, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
          h.update(chunk)
          start += len(chunk)
    s = _session(job["host"])
    headers = {"Range": f"bytes={start}-"} if start else {}
    with s.get(job["download_url"], headers=headers, stream=True, timeout=(30, 60)) as r:
      if r.status_code in (401, 403):
        raise ValueError("Civitai refused the download: set an API key or cookie in Settings"
                         if not _auth() else f"Civitai refused the download (HTTP {r.status_code}): check the API key / cookie")
      if r.status_code != 416 or not start:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype or "json" in ctype:
          raise ValueError(f"expected a file, got {ctype}: {r.text[:200]}")
        if start and r.status_code != 206:
          start, h = 0, hashlib.sha256()
        job["done"] = start
        t0, n0 = time.monotonic(), start
        with open(part, "ab" if start else "wb") as f:
          for chunk in r.iter_content(CHUNK):
            if job["id"] in self.cancel:
              raise Canceled()
            f.write(chunk)
            h.update(chunk)
            job["done"] += len(chunk)
            dt = time.monotonic() - t0
            if dt >= 1:
              job["speed"] = (job["done"] - n0) / dt
              t0, n0 = time.monotonic(), job["done"]
    digest = h.hexdigest()
    if job["sha256"] and digest != job["sha256"]:
      os.remove(part)
      raise ValueError(f"sha256 mismatch ({digest[:12]}...), partial file deleted")
    if os.path.exists(target):
      raise ValueError("file already exists")
    os.rename(part, target)

  def _info(self, job, target):
    if job["note"]:
      # Seed the info file with the note: rgthree's refresh merges into it and pushes the result to
      # open tabs, which keep LoRA info cached until reloaded.
      info_path = target + ".rgthree-info.json"
      _write_json(info_path, {**_read_json(info_path, {}), "userNote": job["note"]})
    try:
      r = _local_get("/rgthree/api/loras/info/refresh", params={"files": os.path.relpath(target, loras_dir())})
    except requests.RequestException as e:
      job["warning"] = f"rgthree info refresh failed ({e}); use Fetch info in the LoRA info dialog"
      return
    if r.status_code == 404:
      job["warning"] = "rgthree not installed: no Civitai info in .rgthree-info.json"
    elif not r.ok:
      job["warning"] = f"rgthree info refresh failed (HTTP {r.status_code}); use Fetch info in the LoRA info dialog"
    else:
      data = (r.json().get("data") or [None])[0] or {}
      if not ((data.get("raw") or {}).get("civitai") or {}).get("modelId"):
        job["warning"] = "rgthree could not fetch Civitai info; use Fetch info in the LoRA info dialog"
