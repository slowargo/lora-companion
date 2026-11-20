"""Find the LoRAs a finished prompt actually used.

Only nodes upstream of the outputs that ran are considered (the API prompt also holds
disconnected nodes and outputs that were not selected). Recognised nodes:
  * rgthree Power Lora Loader: lora_N = {on, lora, strength[, strengthTwo]}
  * any node with a literal `lora_name` input (LoraLoader, LoraLoaderModelOnly, bypass variants,
    KJNodes, ...): skipped when all its numeric strength* inputs are 0.
Plain string inputs elsewhere are ignored on purpose (metadata/saver nodes carry LoRA names too).
"""
import os

PLL_CLASS = "Power Lora Loader (rgthree)"


def _is_link(v):
  return isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int))


def reachable(prompt, outputs):
  seen = set()
  stack = [str(o) for o in outputs]
  while stack:
    nid = stack.pop()
    if nid in seen or nid not in prompt:
      continue
    seen.add(nid)
    for v in (prompt[nid].get("inputs") or {}).values():
      if _is_link(v):
        stack.append(str(v[0]))
  return seen


_index = [None]


def match_lora(name, loras):
  """Same lookup order as rgthree's get_lora_by_filename (exact, no ext, basename, basename no ext)."""
  if not isinstance(name, str) or not name:
    return None
  idx = _index[0]
  if idx is None or idx[0] != loras:
    s = set(loras)
    no_ext, base, base_no_ext = {}, {}, {}
    for p in loras:
      no_ext.setdefault(os.path.splitext(p)[0], p)
      b = os.path.basename(p)
      base.setdefault(b, p)
      base_no_ext.setdefault(os.path.splitext(b)[0], p)
    idx = _index[0] = (list(loras), s, no_ext, base, base_no_ext)
  _, s, no_ext, base, base_no_ext = idx
  if name in s:
    return name
  for table, probe in ((no_ext, name), (no_ext, os.path.splitext(name)[0]), (base, name),
                       (base, os.path.basename(name)), (base_no_ext, name),
                       (base_no_ext, os.path.splitext(os.path.basename(name))[0])):
    hit = table.get(probe)
    if hit:
      return hit
  return None


def _num(v):
  return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def participants(prompt, outputs, cached_nodes, executed):
  """Prompt nodes whose output this run used; empty when nothing was recomputed.

  executed: node ids actually computed this run (None if unknown). Participants are those plus
  the cached nodes they read from. A lazily skipped branch (e.g. the unselected side of a Switch)
  never runs and is only included if it happens to be cached from an earlier run.
  """
  cached = {str(n) for n in (cached_nodes or [])}
  if executed is None:
    nodes = reachable(prompt, outputs)
    return set() if nodes <= cached else nodes
  ran = {str(n) for n in executed if str(n) in prompt}
  if not ran:
    return set()
  return ran | (reachable(prompt, ran) & cached)


def used_loras(prompt, outputs, cached_nodes, loras, executed=None):
  """{relname: (strength, strength_clip)} for one finished run; {} if nothing was recomputed."""
  nodes = participants(prompt, outputs, cached_nodes, executed)
  if not nodes:
    return {}
  found = {}

  def add(name, strength, strength_clip):
    rel = match_lora(name, loras)
    if rel is not None and rel not in found:
      found[rel] = (strength, strength_clip)

  for nid in nodes:
    node = prompt[nid]
    inputs = node.get("inputs") or {}
    if node.get("class_type") == PLL_CLASS:
      # rgthree applies nothing without a model input, and needs on/lora/strength keys.
      if "model" not in inputs:
        continue
      has_clip = "clip" in inputs
      for k, v in inputs.items():
        if not (k.upper().startswith("LORA_") and isinstance(v, dict)):
          continue
        if not v.get("on") or "lora" not in v or "strength" not in v:
          continue
        s = _num(v.get("strength")) or 0
        s2 = _num(v.get("strengthTwo")) if "strengthTwo" in v else s
        s2 = (s2 or 0) if has_clip else 0
        if s != 0 or s2 != 0:
          add(v["lora"], s, s2 if "strengthTwo" in v else None)
      continue
    name = inputs.get("lora_name")
    if not isinstance(name, str):
      continue
    strengths = [_num(v) for k, v in inputs.items() if k.startswith("strength")]
    strengths = [s for s in strengths if s is not None]
    if strengths and all(s == 0 for s in strengths):
      continue
    add(name, _num(inputs.get("strength_model", inputs.get("strength"))), _num(inputs.get("strength_clip")))
  return found
