"""Local ComfyUI image generation via an uploaded API-format workflow JSON."""

from __future__ import annotations

import copy
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from studio.paths import COMFYUI_WORKFLOW_PATH, USER_DATA, ensure_dirs
from studio.settings import COMFYUI_DEFAULT_URL, load_settings, normalize_comfyui_url

log = logging.getLogger("studio.comfyui")

WORKFLOW_PATH = COMFYUI_WORKFLOW_PATH
CONNECT_TIMEOUT = 5.0
SUBMIT_TIMEOUT = 30.0
POLL_TIMEOUT = 600.0
POLL_INTERVAL = 0.75
DOWNLOAD_TIMEOUT = 120.0

NO_WORKFLOW_MESSAGE = (
    "No ComfyUI workflow uploaded. In Settings, upload a ComfyUI API JSON "
    "(ComfyUI File → Save (API Format)). Studio stores it at "
    "user_data/comfyui_workflow.json. ComfyUI jobs never call Fal."
)
NO_WORKFLOW_ERROR = NO_WORKFLOW_MESSAGE
DOWN_MESSAGE = (
    "ComfyUI is not reachable at {url}. Start ComfyUI or check Settings comfyui_url "
    f"(default {COMFYUI_DEFAULT_URL})."
)

_TEXT_KEYS = ("text", "prompt", "text_g", "text_l", "positive", "wildcard_text", "string")
_SEED_KEYS = ("seed", "noise_seed")
_NEG_TITLE = ("negative", "neg prompt", "neg_", "uncond")
_POS_TITLE = ("positive", "pos prompt", "+prompt")
_SAMPLER_TYPES = ("ksampler", "samplercustom", "sampleradvanced", "ksampleradvanced")
_SIZE_CLASS_HINTS = (
    "emptylatent",
    "imageresize",
    "imagescale",
    "latentupscale",
    "emptyimage",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def comfyui_url() -> str:
    return normalize_comfyui_url(load_settings().get("comfyui_url"))


def _is_node(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("class_type"), str)


def extract_prompt_graph(raw: Any) -> dict[str, dict]:
    """Accept Save (API Format) / /prompt graph. Reject the UI workflow editor format."""
    data = raw
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
        maybe = data["prompt"]
        if maybe and all(_is_node(v) for v in maybe.values()):
            data = maybe
    if isinstance(data, dict) and isinstance(data.get("workflow"), dict) and not any(
        _is_node(v) for v in data.values() if isinstance(v, dict)
    ):
        raise RuntimeError(
            "That JSON looks like a ComfyUI UI workflow (nodes/links), not API Format. "
            "In ComfyUI enable 'Enable Dev mode Options', then Save (API Format)."
        )
    if isinstance(data, dict) and isinstance(data.get("nodes"), list) and not any(
        _is_node(v) for v in data.values() if isinstance(v, dict)
    ):
        raise RuntimeError(
            "Upload a ComfyUI API Format JSON (Save (API Format) / /prompt graph), "
            "not the editor workflow with a nodes array."
        )
    if isinstance(data, dict) and data.get("filename") and isinstance(data.get("prompt"), dict):
        data = data["prompt"]
    if not isinstance(data, dict) or not data:
        raise RuntimeError(NO_WORKFLOW_MESSAGE)
    graph: dict[str, dict] = {}
    for key, node in data.items():
        if str(key).startswith("_"):
            continue
        if not _is_node(node):
            continue
        graph[str(key)] = node
    if not graph:
        raise RuntimeError(
            "Upload a ComfyUI API Format JSON (Save (API Format) / /prompt graph). "
            "No nodes with class_type were found."
        )
    return graph


def _node_title(node: dict) -> str:
    meta = node.get("_meta")
    if isinstance(meta, dict):
        title = str(meta.get("title") or "")
        if title:
            return title
    return str(node.get("title") or "")


def _class_key(node: dict) -> str:
    return str(node.get("class_type") or "").replace(" ", "").replace("_", "").lower()


def _inputs(node: dict) -> dict:
    raw = node.get("inputs")
    return raw if isinstance(raw, dict) else {}


def _is_link(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and value and not isinstance(value[0], (dict, list))


def _link_id(value: Any) -> str | None:
    if _is_link(value):
        return str(value[0])
    return None


def _text_fields(node: dict) -> list[str]:
    inputs = _inputs(node)
    class_type = str(node.get("class_type") or "")
    fields: list[str] = []
    for key in _TEXT_KEYS:
        if key in inputs and not _is_link(inputs.get(key)):
            fields.append(key)
    if "CLIPTextEncode" in class_type or class_type.endswith("TextEncode"):
        for key, value in inputs.items():
            if key not in fields and isinstance(value, str):
                fields.append(key)
    return fields


def _looks_negative(node: dict) -> bool:
    blob = f"{_node_title(node)} {node.get('class_type') or ''}".lower()
    return any(hint in blob for hint in _NEG_TITLE)


def _looks_positive(node: dict) -> bool:
    blob = _node_title(node).lower()
    if _looks_negative(node):
        return False
    return any(hint in blob for hint in _POS_TITLE)


def _sampler_linked_ids(graph: dict[str, dict]) -> tuple[set[str], set[str]]:
    positive: set[str] = set()
    negative: set[str] = set()
    for node in graph.values():
        class_key = _class_key(node)
        if not any(hint in class_key for hint in _SAMPLER_TYPES) and "sampler" not in class_key:
            continue
        inputs = _inputs(node)
        pos = _link_id(inputs.get("positive"))
        neg = _link_id(inputs.get("negative"))
        if pos:
            positive.add(pos)
        if neg:
            negative.add(neg)
    return positive, negative


def _candidate_prompt_nodes(graph: dict[str, dict]) -> list[tuple[int, str, dict]]:
    positive_ids, negative_ids = _sampler_linked_ids(graph)
    ranked: list[tuple[int, str, dict]] = []
    for node_id, node in graph.items():
        fields = _text_fields(node)
        if not fields:
            continue
        title = _node_title(node)
        class_type = str(node.get("class_type") or "")
        is_clip = "CLIPTextEncode" in class_type or class_type.endswith("TextEncode")
        is_neg = _looks_negative(node) or node_id in negative_ids
        is_pos = (
            node_id in positive_ids
            or _looks_positive(node)
            or (is_clip and not is_neg)
        )
        text_val = ""
        for field in fields:
            raw = _inputs(node).get(field)
            if isinstance(raw, str) and raw.strip():
                text_val = raw.strip()
                break
        score = 0
        if is_neg:
            score -= 100
        if node_id in positive_ids:
            score += 80
        if _looks_positive(node):
            score += 40
        if is_clip:
            score += 20
        if text_val:
            score += 10
        if "prompt" in title.lower() and not is_neg:
            score += 15
        ranked.append((score, node_id, node))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def find_prompt_node(graph: dict[str, dict]) -> tuple[str, dict, list[str]]:
    ranked = _candidate_prompt_nodes(graph)
    for score, node_id, node in ranked:
        if score < 0:
            continue
        fields = _text_fields(node)
        if fields:
            return node_id, node, fields
    if ranked:
        node_id, node = ranked[0][1], ranked[0][2]
        fields = _text_fields(node)
        if fields:
            return node_id, node, fields
    raise RuntimeError(
        "Could not find a CLIPTextEncode / text widget in the uploaded ComfyUI API workflow. "
        "The graph needs a positive prompt node with a text or prompt field."
    )


def find_size_nodes(graph: dict[str, dict]) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for node_id, node in graph.items():
        inputs = _inputs(node)
        if "width" not in inputs or "height" not in inputs:
            continue
        if _is_link(inputs.get("width")) or _is_link(inputs.get("height")):
            continue
        class_key = _class_key(node)
        if any(hint in class_key for hint in _SIZE_CLASS_HINTS) or (
            isinstance(inputs.get("width"), (int, float, str))
            and isinstance(inputs.get("height"), (int, float, str))
        ):
            if "scaleby" in class_key and "resize" not in class_key:
                continue
            found.append((node_id, node))
    return found


def describe_prompt_mapping(graph: dict[str, dict] | None = None) -> dict[str, Any]:
    graph = graph if graph is not None else loaded_prompt_graph()
    if not graph:
        return {"found": False, "node_id": None, "class_type": None, "title": None, "fields": []}
    try:
        node_id, node, fields = find_prompt_node(graph)
    except RuntimeError as exc:
        return {"found": False, "error": str(exc), "node_id": None, "fields": []}
    return {
        "found": True,
        "node_id": node_id,
        "class_type": node.get("class_type"),
        "title": _node_title(node) or None,
        "fields": fields,
        "note": (
            "Studio writes the current illustration prompt into this node's "
            f"{', '.join(fields)} input(s). Prefers a CLIPTextEncode titled or linked as "
            "the sampler positive prompt; otherwise the first positive / non-empty text widget."
        ),
    }


def inject_prompt(graph: dict[str, dict], prompt: str) -> dict[str, Any]:
    node_id, node, fields = find_prompt_node(graph)
    inputs = _inputs(node)
    for field in fields:
        if field in inputs and not _is_link(inputs.get(field)):
            inputs[field] = prompt
    if "text" not in fields and "text" in inputs and not _is_link(inputs.get("text")):
        inputs["text"] = prompt
    node["inputs"] = inputs
    return {"node_id": node_id, "fields": fields, "class_type": node.get("class_type"), "title": _node_title(node)}


def inject_size(graph: dict[str, dict], width: int, height: int) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for node_id, node in find_size_nodes(graph):
        inputs = _inputs(node)
        inputs["width"] = int(width)
        inputs["height"] = int(height)
        node["inputs"] = inputs
        changed.append(
            {
                "node_id": node_id,
                "class_type": node.get("class_type"),
                "title": _node_title(node) or None,
                "width": int(width),
                "height": int(height),
            }
        )
    return changed


def _randomize_seeds(graph: dict[str, dict]) -> None:
    seed = random.randint(0, 2**31 - 1)
    for node in graph.values():
        inputs = _inputs(node)
        dirty = False
        for key in _SEED_KEYS:
            if key in inputs and not _is_link(inputs.get(key)):
                inputs[key] = seed
                dirty = True
        if dirty:
            node["inputs"] = inputs


def _wrapper_payload(filename: str, graph: dict[str, dict], uploaded_at: str | None = None) -> dict[str, Any]:
    mapping = describe_prompt_mapping(graph)
    sizes = [
        {"node_id": nid, "class_type": node.get("class_type"), "title": _node_title(node) or None}
        for nid, node in find_size_nodes(graph)
    ]
    return {
        "filename": filename,
        "uploaded_at": uploaded_at or _now_iso(),
        "prompt": graph,
        "prompt_node": mapping,
        "size_nodes": sizes,
    }


def loaded_record() -> dict[str, Any] | None:
    ensure_dirs()
    if not WORKFLOW_PATH.is_file():
        return None
    try:
        raw = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        graph = extract_prompt_graph(raw)
    except RuntimeError:
        return None
    if isinstance(raw, dict) and raw.get("filename") and isinstance(raw.get("prompt"), dict):
        record = dict(raw)
        record["prompt"] = graph
        return record
    return _wrapper_payload(WORKFLOW_PATH.name, graph)


def loaded_prompt_graph() -> dict[str, dict] | None:
    record = loaded_record()
    if not record:
        return None
    graph = record.get("prompt")
    if isinstance(graph, dict) and graph:
        return extract_prompt_graph(graph)
    return None


def workflow_is_loaded() -> bool:
    return loaded_prompt_graph() is not None


def workflow_public_status() -> dict[str, Any]:
    record = loaded_record()
    if not record:
        return {
            "loaded": False,
            "filename": "",
            "uploaded_at": "",
            "path": str(WORKFLOW_PATH),
            "prompt_node": {"found": False},
            "size_nodes": [],
            "node_count": 0,
        }
    graph = record.get("prompt") if isinstance(record.get("prompt"), dict) else {}
    mapping = record.get("prompt_node") if isinstance(record.get("prompt_node"), dict) else describe_prompt_mapping(graph)
    return {
        "loaded": True,
        "filename": record.get("filename") or WORKFLOW_PATH.name,
        "uploaded_at": record.get("uploaded_at") or "",
        "path": str(WORKFLOW_PATH),
        "prompt_node": mapping,
        "size_nodes": record.get("size_nodes") or [
            {"node_id": nid, "class_type": node.get("class_type")}
            for nid, node in find_size_nodes(graph)
        ],
        "node_count": len(graph),
    }


def list_workflows() -> dict[str, Any]:
    status = workflow_public_status()
    items = []
    if status.get("loaded"):
        items.append(
            {
                "filename": status.get("filename"),
                "uploaded_at": status.get("uploaded_at"),
                "path": status.get("path"),
                "node_count": status.get("node_count"),
                "prompt_node": status.get("prompt_node"),
            }
        )
    return {"workflows": items, "loaded": status.get("loaded"), **status}


def save_workflow(raw: Any, filename: str = "") -> dict[str, Any]:
    graph = extract_prompt_graph(raw)
    find_prompt_node(graph)
    name = Path(filename or "comfyui_workflow.json").name
    if not name.lower().endswith(".json"):
        name = f"{name}.json"
    ensure_dirs()
    payload = _wrapper_payload(name, graph)
    WORKFLOW_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        from studio.settings import save_settings

        save_settings({"comfyui_workflow_filename": name})
    except Exception:
        pass
    return workflow_public_status()


def delete_workflow() -> dict[str, Any]:
    if WORKFLOW_PATH.is_file():
        WORKFLOW_PATH.unlink()
    try:
        from studio.settings import save_settings

        save_settings({"comfyui_workflow_filename": ""})
    except Exception:
        pass
    return workflow_public_status()


def comfyui_status() -> dict[str, Any]:
    wf = workflow_public_status()
    url = comfyui_url()
    reachable = False
    error = None
    try:
        ping_comfyui(url)
        reachable = True
    except Exception as exc:
        error = str(exc)
    return {
        "url": url,
        "reachable": reachable,
        "workflow_loaded": bool(wf.get("loaded")),
        "workflow_filename": wf.get("filename") or "",
        "ok": reachable and bool(wf.get("loaded")),
        "error": None if reachable else error,
        **wf,
    }


def _http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(SUBMIT_TIMEOUT, connect=CONNECT_TIMEOUT))


def ping_comfyui(url: str | None = None) -> None:
    base = normalize_comfyui_url(url or comfyui_url())
    try:
        with httpx.Client(timeout=httpx.Timeout(CONNECT_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
            for path in ("/system_stats", "/queue", "/object_info"):
                try:
                    response = client.get(f"{base}{path}")
                except httpx.HTTPError:
                    continue
                if response.status_code < 500:
                    return
    except httpx.HTTPError as exc:
        raise RuntimeError(DOWN_MESSAGE.format(url=base)) from exc
    raise RuntimeError(DOWN_MESSAGE.format(url=base))


def ensure_comfyui_ready() -> dict[str, Any]:
    if not workflow_is_loaded():
        raise RuntimeError(NO_WORKFLOW_MESSAGE)
    ping_comfyui()
    return workflow_public_status()


def _history_images(entry: dict) -> list[dict[str, str]]:
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    if not isinstance(outputs, dict):
        return []
    images: list[dict[str, str]] = []
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        blob = node_out.get("images") or node_out.get("gifs") or []
        if not isinstance(blob, list):
            continue
        for item in blob:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not filename:
                continue
            images.append(
                {
                    "filename": str(filename),
                    "subfolder": str(item.get("subfolder") or ""),
                    "type": str(item.get("type") or "output"),
                }
            )
    return images


def _history_failed(entry: dict) -> str | None:
    status = entry.get("status") if isinstance(entry, dict) else None
    if not isinstance(status, dict):
        return None
    flag = str(status.get("status_str") or "").lower()
    if status.get("completed") and flag in ("error", "interrupted"):
        messages = status.get("messages") or []
        return f"ComfyUI job {flag}: {messages!r}"
    if flag in ("error", "interrupted"):
        return f"ComfyUI job {flag}."
    return None


def _submit_prompt(base: str, graph: dict[str, dict], client: httpx.Client) -> str:
    payload = {"prompt": graph, "client_id": str(uuid.uuid4())}
    try:
        response = client.post(f"{base}/prompt", json=payload, timeout=SUBMIT_TIMEOUT)
    except httpx.ConnectError as exc:
        raise RuntimeError(DOWN_MESSAGE.format(url=base)) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(DOWN_MESSAGE.format(url=base)) from exc
    if response.status_code >= 500:
        raise RuntimeError(DOWN_MESSAGE.format(url=base) + f" HTTP {response.status_code}.")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"ComfyUI /prompt returned non-JSON ({response.status_code}).") from exc
    errors = data.get("node_errors") if isinstance(data, dict) else None
    if errors:
        raise RuntimeError(f"ComfyUI rejected the workflow: {errors}")
    prompt_id = (data or {}).get("prompt_id") if isinstance(data, dict) else None
    if not prompt_id:
        detail = data.get("error") if isinstance(data, dict) else data
        raise RuntimeError(f"ComfyUI /prompt did not return prompt_id: {detail}")
    return str(prompt_id)


def interrupt_comfyui(base: str | None = None) -> bool:
    """POST /interrupt (and best-effort queue clear) so an in-flight prompt stops."""
    url = normalize_comfyui_url(base or comfyui_url())
    ok = False
    try:
        with _http() as client:
            try:
                response = client.post(f"{url}/interrupt", timeout=CONNECT_TIMEOUT)
                ok = response.status_code < 500
            except Exception as exc:
                log.info("ComfyUI interrupt failed: %s", exc)
            try:
                client.post(f"{url}/queue", json={"clear": True}, timeout=CONNECT_TIMEOUT)
            except Exception:
                pass
    except Exception as exc:
        log.info("ComfyUI interrupt unreachable: %s", exc)
    return ok


def _poll_history(
    base: str,
    prompt_id: str,
    client: httpx.Client,
    cancel_check: Any = None,
) -> list[dict[str, str]]:
    deadline = time.time() + POLL_TIMEOUT
    last_error = None
    while time.time() < deadline:
        if cancel_check:
            cancel_check()
        try:
            response = client.get(f"{base}/history/{prompt_id}", timeout=CONNECT_TIMEOUT + 5)
        except httpx.ConnectError as exc:
            raise RuntimeError(DOWN_MESSAGE.format(url=base)) from exc
        except httpx.TimeoutException:
            time.sleep(POLL_INTERVAL)
            continue
        if response.status_code == 404:
            time.sleep(POLL_INTERVAL)
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            time.sleep(POLL_INTERVAL)
            continue
        try:
            data = response.json()
        except Exception:
            time.sleep(POLL_INTERVAL)
            continue
        entry = data.get(prompt_id) if isinstance(data, dict) else None
        if not isinstance(entry, dict) and isinstance(data, dict) and data.get("outputs"):
            entry = data
        if not isinstance(entry, dict):
            time.sleep(POLL_INTERVAL)
            continue
        failed = _history_failed(entry)
        if failed:
            raise RuntimeError(failed)
        images = _history_images(entry)
        if images:
            return images
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(
        f"Timed out after {int(POLL_TIMEOUT)}s waiting for ComfyUI prompt {prompt_id}"
        + (f" ({last_error})." if last_error else ".")
    )


def _download_image(base: str, spec: dict[str, str], client: httpx.Client) -> bytes:
    query = urlencode(
        {
            "filename": spec["filename"],
            "subfolder": spec.get("subfolder") or "",
            "type": spec.get("type") or "output",
        }
    )
    try:
        response = client.get(f"{base}/view?{query}", timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Failed to download ComfyUI image {spec.get('filename')}: {exc}") from exc
    if not response.content or len(response.content) < 32:
        raise RuntimeError("ComfyUI /view returned an empty image.")
    return response.content


def generate_image_bytes(
    prompt: str,
    width: int,
    height: int,
    cancel_check: Any = None,
    on_submitted: Any = None,
) -> bytes:
    """POST the uploaded workflow to ComfyUI /prompt, poll /history, download /view."""
    from studio.gpu_lock import holding

    with holding("comfyui", kind="comfyui"):
        return _generate_image_bytes_unlocked(
            prompt, width, height, cancel_check=cancel_check, on_submitted=on_submitted
        )


def _generate_image_bytes_unlocked(
    prompt: str,
    width: int,
    height: int,
    cancel_check: Any = None,
    on_submitted: Any = None,
) -> bytes:
    """POST the uploaded workflow to ComfyUI /prompt, poll /history, download /view."""
    text = (prompt or "").strip()
    if not text:
        raise RuntimeError("Illustration prompt is empty.")
    graph = loaded_prompt_graph()
    if not graph:
        raise RuntimeError(NO_WORKFLOW_MESSAGE)
    base = comfyui_url()
    ping_comfyui(base)
    work = copy.deepcopy(graph)
    mapping = inject_prompt(work, text)
    sizes = inject_size(work, int(width), int(height))
    _randomize_seeds(work)
    log.info(
        "ComfyUI prompt node %s (%s) fields=%s size_nodes=%s %sx%s (%s chars)",
        mapping.get("node_id"),
        mapping.get("class_type"),
        mapping.get("fields"),
        [item.get("node_id") for item in sizes],
        width,
        height,
        len(text),
    )
    if cancel_check:
        cancel_check()
    with _http() as client:
        prompt_id = _submit_prompt(base, work, client)
        if on_submitted:
            try:
                on_submitted(prompt_id)
            except Exception:
                pass
        try:
            images = _poll_history(base, prompt_id, client, cancel_check=cancel_check)
            return _download_image(base, images[-1], client)
        except Exception:
            try:
                interrupt_comfyui(base)
            except Exception:
                pass
            raise
