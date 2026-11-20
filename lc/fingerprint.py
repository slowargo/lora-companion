"""Cheap content fingerprint for model files.

Full sha256 of multi-GB files is slow and competes with model loading for the disk. Instead hash
the file size, the whole safetensors header (tensor names, shapes, offsets and __metadata__) and
three 64KB samples of the data section (start, middle, end). Two different LoRAs of the same
architecture can share a header, but not the sampled data.
"""
import hashlib
import json
import os

VERSION = "fp1"
SAMPLE = 64 * 1024
MAX_HEADER = 100_000_000  # safetensors caps the header at 100MB


def read_safetensors_header(f, size):
  """Returns (header_bytes, data_start) or (None, 0) when the file is not a safetensors file."""
  f.seek(0)
  raw = f.read(8)
  if len(raw) != 8:
    return None, 0
  n = int.from_bytes(raw, "little", signed=False)
  if not 0 < n <= min(MAX_HEADER, size - 8):
    return None, 0
  header = f.read(n)
  if len(header) != n or not header.lstrip().startswith(b"{"):
    return None, 0
  return header, 8 + n


def fingerprint(path):
  size = os.path.getsize(path)
  h = hashlib.sha256()
  h.update(size.to_bytes(8, "little"))
  with open(path, "rb") as f:
    header, data_start = read_safetensors_header(f, size)
    if header is not None:
      h.update(header)
    data_len = size - data_start
    if data_len <= 3 * SAMPLE:
      offsets = [data_start]
      lengths = [data_len]
    else:
      offsets = [data_start, data_start + (data_len - SAMPLE) // 2, size - SAMPLE]
      lengths = [SAMPLE] * 3
    for off, ln in zip(offsets, lengths):
      f.seek(off)
      h.update(f.read(ln))
  return f"{VERSION}:{h.hexdigest()}"


# --- LoRA type from header key names ---

# Mirrors the key names comfy/lora.py and comfy/weight_adapter look for. Order sensitive types read
# or multiply the current weight, so the result depends on what was patched before them.
_LORA_KEY_TYPES = (
  ('LoRA', ('lora_up.weight', 'lora.up.weight', '.lora_B.weight', '.lora_B', '.lora_B.default.weight', '.lora_linear_layer.up.weight'), False),
  ('LoHa', ('.hada_w1_a',), False),
  ('LoKr', ('.lokr_w1', '.lokr_w1_a', '.lokr_w2', '.lokr_w2_a'), False),
  ('GLoRA', ('.a1.weight',), True),
  ('Diff', ('.diff', '.diff_b', '.w_norm', '.b_norm'), False),
  ('Set', ('.set_weight',), True),
)


def lora_type(path):
  """Adapter type(s) of a LoRA file from its safetensors header key names, or None."""
  if not path.lower().endswith(('.safetensors', '.sft')):
    return None
  try:
    with open(path, "rb") as f:
      header, _ = read_safetensors_header(f, os.path.getsize(path))
    if header is None:
      return None
    keys = json.loads(header)
  except (OSError, ValueError, RecursionError):
    return None
  if not isinstance(keys, dict):
    return None
  keys.pop('__metadata__', None)
  # BFL Flux control LoRAs are renamed by comfy/lora_convert.py before loading.
  if 'img_in.lora_A.weight' in keys and 'single_blocks.0.norm.key_norm.scale' in keys:
    keys = {
      k.replace('.lora_B.bias', '.diff_b').replace('_norm.scale', '_norm.set_weight'): v
      for k, v in keys.items()
    }
  types = []
  order_sensitive = False
  for name, suffixes, sensitive in _LORA_KEY_TYPES:
    if any(key.endswith(suffixes) for key in keys):
      types.append(name)
      order_sensitive = order_sensitive or sensitive
  # ComfyUI tells OFT and BOFT apart by the rank of the oft_blocks tensor.
  oft_ranks = {
    len(v['shape']) for k, v in keys.items()
    if k.endswith('.oft_blocks') and isinstance(v, dict) and isinstance(v.get('shape'), list)
  }
  if 3 in oft_ranks:
    types.append('OFT')
  if 4 in oft_ranks:
    types.append('BOFT')
  if oft_ranks:
    order_sensitive = True
  dora = any(key.endswith('.dora_scale') for key in keys)
  if dora:
    order_sensitive = True
  label = ' + '.join(types) or 'Unknown'
  if dora:
    label += ' (DoRA)'
  return {'label': label, 'orderSensitive': order_sensitive}
