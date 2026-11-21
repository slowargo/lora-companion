"""lora-companion: LoRA usage counts, like/dislike counters and Power Lora Loader UI extras.

No graph nodes. Backend: counts successful runs per LoRA (SQLite in user/lora-companion/stats.db)
and serves /lora-companion/* routes. Frontend (web/lora_companion.js) extends rgthree's
Power Lora Loader info dialog and LoRA chooser without patching rgthree.
"""
import asyncio
import concurrent.futures
import logging
import os
import threading

import folder_paths
import execution
from aiohttp import web
from server import PromptServer

from .lc.store import Store, LOG
from .lc.scan import used_loras
from .lc.fingerprint import lora_type
from .lc import backfill

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

STORE = Store(os.path.join(folder_paths.get_user_directory(), "lora-companion", "stats.db"))
_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="lora-companion")


def _push(stats):
  if stats:
    PromptServer.instance.send_sync("lora-companion.stats", {"stats": stats})


def _record(prompt, prompt_id, outputs, cached, executed):
  try:
    uses = used_loras(prompt, outputs, cached, folder_paths.get_filename_list("loras"), executed)
    if uses:
      _push(STORE.record_uses(uses, prompt_id))
    else:
      STORE.last_prompt_id = prompt_id
  except Exception:
    logging.exception("%s failed to record LoRA usage", LOG)


def _after_execute(executor, prompt, prompt_id, outputs, executed):
  ok = False
  cached = []
  for event, data in getattr(executor, "status_messages", None) or []:
    if data.get("prompt_id") != prompt_id:
      continue
    if event == "execution_success":
      ok = True
    elif event == "execution_cached":
      cached = data.get("nodes") or []
  if ok:
    _worker.submit(_record, prompt, prompt_id, list(outputs or []), cached,
                   None if executed is None else set(executed))


# The per-node execution.execute() gets the prompt's `executed` set (nodes actually computed this
# run; cache hits are served without it and lazily skipped branches never run). Capture that set
# object so the run's real participants are known.
_tls = threading.local()
_orig_execute_node = execution.execute


async def _execute_node(*args, **kwargs):
  if getattr(_tls, "capturing", False):
    _tls.calls += 1
  if getattr(_tls, "capturing", False) and _tls.executed is None:
    ex = args[5] if len(args) > 5 else kwargs.get("executed")
    if isinstance(ex, set):
      _tls.executed = ex
  return await _orig_execute_node(*args, **kwargs)


execution.execute = _execute_node
_warned = []


def _warn_capture_once():
  if not _warned:
    _warned.append(1)
    logging.warning("%s could not capture the executed-node set; counting by graph reachability", LOG)

_orig_execute = execution.PromptExecutor.execute


def _execute(self, prompt, prompt_id, extra_data={}, execute_outputs=[], *args, **kwargs):
  _tls.capturing = True
  _tls.executed = None
  _tls.calls = 0
  try:
    result = _orig_execute(self, prompt, prompt_id, extra_data, execute_outputs, *args, **kwargs)
  finally:
    _tls.capturing = False
  executed = _tls.executed
  _tls.executed = None
  if executed is None:
    if _tls.calls == 0:
      executed = set()  # nothing ran: every output was cached
    else:
      _warn_capture_once()  # execution.execute changed its signature: fall back to graph walk
  try:
    _after_execute(self, prompt, prompt_id, execute_outputs, executed)
  except Exception:
    logging.exception("%s post-execute hook failed", LOG)
  return result


execution.PromptExecutor.execute = _execute

# Fingerprint all LoRA files once in the background so the chooser counts and renamed files are
# ready without a delay on first use. Delayed so it does not compete with startup model loads.
_warm = threading.Timer(20.0, lambda: _worker.submit(STORE.warm))
_warm.daemon = True
_warm.start()

routes = PromptServer.instance.routes


def _run(fn, *args):
  return asyncio.get_running_loop().run_in_executor(_worker, fn, *args)


@routes.get("/lora-companion/stats")
async def api_all_stats(request):
  return web.json_response({"stats": await _run(STORE.all_stats)})


@routes.get("/lora-companion/info")
async def api_info(request):
  file = request.query.get("file")
  if not file:
    return web.json_response({"error": "file required"}, status=400)

  def work():
    full = folder_paths.get_full_path("loras", file)
    if full is None:
      return None
    return {"file": file, "stats": STORE.stats(file), "loraType": lora_type(full)}

  data = await _run(work)
  if data is None:
    return web.json_response({"error": "not found"}, status=404)
  return web.json_response(data)


@routes.post("/lora-companion/backfill")
async def api_backfill(request):
  """Backfill uses from output PNG metadata. ?apply=1 writes; default is a dry run.
  Optional JSON body {"aliases": {"old lora name": "current relative path"}} for renamed files."""
  apply = request.query.get("apply") == "1"
  try:
    body = await request.json() if request.can_read_body else {}
  except ValueError:
    return web.json_response({"error": "bad request"}, status=400)
  aliases = body.get("aliases") if isinstance(body, dict) else None
  if aliases is not None and not (isinstance(aliases, dict)
                                  and all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items())):
    return web.json_response({"error": "aliases must map old name -> current name"}, status=400)
  summary = await _run(backfill.run, STORE, not apply, None, aliases)
  if apply:
    await _run(lambda: _push(STORE.all_stats()))
  return web.json_response(summary)


@routes.post("/lora-companion/vote")
async def api_vote(request):
  try:
    body = await request.json()
  except ValueError:
    return web.json_response({"error": "bad request"}, status=400)
  if not isinstance(body, dict):
    return web.json_response({"error": "bad request"}, status=400)
  file, kind, delta = body.get("file"), body.get("kind"), body.get("delta")
  if kind not in ("like", "dislike") or delta not in (1, -1) or not isinstance(file, str):
    return web.json_response({"error": "bad request"}, status=400)
  stats = await _run(STORE.vote, file, kind, delta)
  if stats is None:
    return web.json_response({"error": "not found"}, status=404)
  _push({file: stats})
  return web.json_response({"file": file, "stats": stats})
