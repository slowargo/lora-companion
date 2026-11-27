# lora-companion

LoRA usage statistics, a Civitai downloader and quality-of-life additions for
[rgthree's Power Lora Loader](https://github.com/rgthree/rgthree-comfy), without patching rgthree.
Adds no graph nodes.

## Features

- **Usage count** per LoRA, counted once per successful run.
  - Only LoRAs that actually took part in the run are counted. Loaders that are not connected to an
    executed output, the unselected branch of a lazy Switch, and LoRAs that are off or at strength 0
    are not counted.
  - Runs where every node was served from cache count nothing.
  - Recognised loaders: Power Lora Loader, and any node with a `lora_name` input (`LoraLoader`,
    `LoraLoaderModelOnly`, and similar).
- **Like / dislike counters** in the Power Lora Loader LoRA info dialog (`+` / `−`, never below 0).
- **LoRA chooser** shows `(uses · 👍likes 👎dislikes)` after each LoRA name (zero counts omitted) and scrolls to and highlights the row's
  current LoRA. Switching an existing row to a different LoRA resets its toggle to on and its strength(s) to 1.
- **Info dialog extras**:
  - LoRA type (LoRA / LoHa / LoKr / OFT / DoRA …, read from the safetensors header) and whether its
    order in the loader matters.
  - Civitai version description as plain text.
  - Civitai links open civitai.red.
  - The Additional Notes edit box keeps the existing text; Ctrl+Enter (Cmd+Enter) saves it.
- **Info icon fix**: with the current ComfyUI frontend, Power Lora Loader rows never load their LoRA
  info after a workflow is opened, so every info icon is gray and the strength min/max hints are
  missing. Rows now load their info when drawn, again when the LoRA changes, and after the info is
  refreshed (for example by a Civitai fetch in the info dialog).

Counts update live in all open tabs after each run or vote.

## Civitai downloader

`http://127.0.0.1:8188/lora-companion/downloader` downloads a LoRA from a civitai.com or
civitai.red model URL into a subfolder of the loras folder.

- The default file name is the model title plus the original file name. Words listed in Settings and
  the version's base model name (e.g. `Krea 2`) are removed from the title. The name can be edited.
- After the download (sha256 checked) rgthree builds the `.rgthree-info.json`, and the page's note
  is saved as its Additional Notes.
- Submit as many downloads as you like; they run one at a time. The queue is kept in
  `user/lora-companion/downloads.json`, and an interrupted download resumes after a restart.
- Settings (`user/lora-companion/downloader.json`): root folder relative to the loras folder, hidden
  subfolders, title words to remove. Most Civitai downloads need an API key (Civitai account
  settings → API Keys) or a logged-in Cookie header; it is stored in
  `user/lora-companion/civitai_auth.txt` and only sent to the Civitai host of the URL.

## Install

Clone into `ComfyUI/custom_nodes/` and restart ComfyUI. Requires
[rgthree-comfy](https://github.com/rgthree/rgthree-comfy). No extra Python dependencies.

## Storage

`ComfyUI/user/lora-companion/stats.db` (SQLite). Each use or vote is stored as an event.

LoRA files are identified by a content fingerprint: a hash of the file size, the safetensors header
and three 64KB samples of the data. Hashing a file takes milliseconds, and renamed or moved LoRAs keep
their stats. Two files with identical content share one set of stats.

## Backfill from existing images

Counts past runs from the prompt metadata embedded in PNGs in the output folder:

```sh
curl -X POST "http://127.0.0.1:8188/lora-companion/backfill"           # dry run, prints a summary
curl -X POST "http://127.0.0.1:8188/lora-companion/backfill?apply=1"   # write
```

- Images with the same prompt count as one run.
- Only images older than the first live count are used.
- Running the backfill again does not count a run twice.
- LoRAs that were renamed since can be mapped with a JSON body:
  `{"aliases": {"old name.safetensors": "folder/new name.safetensors"}}`.
  The dry run lists names it could not match.

To undo: `DELETE FROM events WHERE source='backfill'`, then restart ComfyUI.

## API

| Route | |
|---|---|
| `GET /lora-companion/stats` | `{stats: {lora: {uses, likes, dislikes, last_used}}}` for every LoRA with events |
| `GET /lora-companion/info?file=<lora>` | stats and LoRA type of one file |
| `POST /lora-companion/vote` | body `{file, kind: "like" \| "dislike", delta: 1 \| -1}` |
| `POST /lora-companion/backfill` | see above |

Websocket event `lora-companion.stats` carries `{stats: {lora: stats}}` for changed LoRAs.

## Compatibility

The frontend hooks into rgthree internals: `RgthreeLoraInfoDialog` in `dialog_info.js`, the Power
Lora Loader node prototype, and the "Choose a lora" context menu. If an rgthree update changes them,
the affected feature is skipped with a `[lora-companion]` warning in the browser console, and rgthree
keeps working. Counting runs on the server and is not affected.
