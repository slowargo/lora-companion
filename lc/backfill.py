"""Backfill LoRA uses from the API prompt embedded in output PNGs.

One run = one distinct prompt JSON (copies of an image, several outputs of one run and batch images
carry the same prompt). Idempotent: each run is stored with prompt_id "bf:<sha1 of prompt>" and
skipped if already present. Only images older than the first live event are used, so runs counted
live are not counted again.
Without the executed-node set, participants are found by walking up from the output nodes
(nodes.NODE_CLASS_MAPPINGS[...].OUTPUT_NODE; unknown classes count as outputs when nothing reads
them). Known gaps: deleted images are lost; LoRAs renamed since then are not matched; the
unselected side of a lazy Switch is counted; reruns of an identical prompt count once.
"""
import hashlib
import json
import logging
import os
import time

import folder_paths
import nodes
from PIL import Image

from .scan import used_loras, match_lora
from .store import LOG


def _output_nodes(prompt):
  mappings = nodes.NODE_CLASS_MAPPINGS
  consumed = set()
  for node in prompt.values():
    for v in (node.get("inputs") or {}).values():
      if isinstance(v, list) and len(v) == 2:
        consumed.add(str(v[0]))
  outs = []
  for nid, node in prompt.items():
    cls = mappings.get(node.get("class_type"))
    if cls is not None:
      if getattr(cls, "OUTPUT_NODE", False):
        outs.append(nid)
    elif nid not in consumed:
      outs.append(nid)
  return outs


def _read_prompt(path):
  with Image.open(path) as im:
    text = im.info.get("prompt")
  if not text:
    return None, None
  return text, json.loads(text)


def run(store, dry_run=True, root=None, aliases=None):
  aliases = aliases or {}
  root = root or folder_paths.get_output_directory()
  cutoff = store.first_live_ts() or time.time()
  loras = folder_paths.get_filename_list("loras")
  t0 = time.perf_counter()
  runs = {}  # sha1 -> [min_mtime, prompt, files]
  summary = {"images": 0, "no_prompt": 0, "unreadable": 0, "after_cutoff": 0}
  for dp, _, files in os.walk(root):
    for f in files:
      if not f.lower().endswith(".png"):
        continue
      p = os.path.join(dp, f)
      try:
        mtime = os.path.getmtime(p)
        if mtime >= cutoff:
          summary["after_cutoff"] += 1
          continue
        text, prompt = _read_prompt(p)
      except Exception:
        summary["unreadable"] += 1
        continue
      if not isinstance(prompt, dict):
        summary["no_prompt"] += 1
        continue
      summary["images"] += 1
      h = hashlib.sha1(text.encode("utf-8")).hexdigest()
      r = runs.get(h)
      if r is None:
        runs[h] = [mtime, prompt, 1]
      else:
        r[0] = min(r[0], mtime)
        r[2] += 1

  unmatched = {}
  plan = []  # (ts, prompt_id, {relname: (s, s2)})
  for h, (mtime, prompt, _) in runs.items():
    prompt = {str(k): v for k, v in prompt.items() if isinstance(v, dict)}
    if aliases:
      _apply_aliases(prompt, aliases)
    uses = used_loras(prompt, _output_nodes(prompt), [], loras, None)
    for name in _referenced_names(prompt):
      if match_lora(name, loras) is None:
        unmatched[name] = unmatched.get(name, 0) + 1
    if uses:
      plan.append((mtime, "bf:" + h, uses))

  summary.update({
    "runs": len(runs),
    "runs_with_loras": len(plan),
    "lora_uses": sum(len(u) for _, _, u in plan),
    "cutoff": cutoff,
    "unmatched_names": dict(sorted(unmatched.items(), key=lambda kv: -kv[1])[:30]),
    "unmatched_total": len(unmatched),
    "seconds": round(time.perf_counter() - t0, 1),
  })
  if not dry_run:
    summary["inserted_runs"] = store.add_backfill(plan)
    logging.info("%s backfill: %s", LOG, {k: v for k, v in summary.items() if k != "unmatched_names"})
  return summary


def _apply_aliases(prompt, aliases):
  """Rewrite old LoRA names (renamed files) to their current relative path, in place."""
  for node in prompt.values():
    inputs = node.get("inputs") or {}
    v = inputs.get("lora_name")
    if isinstance(v, str) and v in aliases:
      inputs["lora_name"] = aliases[v]
    for k, v in inputs.items():
      if k.upper().startswith("LORA_") and isinstance(v, dict) and v.get("lora") in aliases:
        inputs[k] = {**v, "lora": aliases[v["lora"]]}


def _referenced_names(prompt):
  """LoRA names mentioned by recognised loader nodes (for the unmatched report only)."""
  out = set()
  for node in prompt.values():
    inputs = node.get("inputs") or {}
    if isinstance(inputs.get("lora_name"), str):
      out.add(inputs["lora_name"])
    if node.get("class_type") == "Power Lora Loader (rgthree)":
      for k, v in inputs.items():
        if k.upper().startswith("LORA_") and isinstance(v, dict) and v.get("on") and isinstance(v.get("lora"), str):
          out.add(v["lora"])
  out.discard("None")
  return out
