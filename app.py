"""Serve WoodCraft and queue Blender renders from scene data."""
import json
import hmac
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

import modal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

ROOT = Path(__file__).resolve().parent
RUNTIME = Path("/opt/woodcraft")
app = modal.App("woodcraft-blender")
web_image = (modal.Image.debian_slim(python_version="3.12")
             .pip_install("fastapi[standard]")
             .add_local_file(ROOT / "index.html", str(RUNTIME / "index.html")))
render_image = (modal.Image.from_registry("python:3.12-slim-trixie")
                .apt_install("blender", "fonts-dejavu-core")
                .pip_install("fastapi")
                .add_local_file(ROOT / "render_scene.py", str(RUNTIME / "render_scene.py"))
                .add_local_file(ROOT / "assets.json", str(RUNTIME / "assets.json")))
TOKEN = modal.Secret.from_name("woodcraft-access", required_keys=["WOODCRAFT_ACCESS_KEY"])
HEX = re.compile(r"#[A-Fa-f0-9]{6}")
MAX_BODY = 4 * 1024 * 1024
MODEL_COUNTS = {"shapr-three-drawer": 6, "shapr-one-door": 6, "shapr-oven": 14}


def require(condition):
    if not condition:
        raise ValueError("Invalid scene")


def number(value, low, high):
    require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high)


def vec(value, low, high):
    require(isinstance(value, list) and len(value) == 3)
    for item in value:
        number(item, low, high)


def validate_scene(scene):
    require(isinstance(scene, dict))
    require(isinstance(scene["project"], str) and len(scene["project"]) <= 2000)
    require(isinstance(scene["template"], str) and len(scene["template"]) <= 100)
    require(scene["studio"] in ("studio", "room", "kitchen"))
    require(scene["samples"] in (32, 128, 512))
    for axis in "WHD":
        number(scene["dims_mm"][axis], .1, 250000)
    materials = scene["materials"]
    require(isinstance(materials, dict) and 0 < len(materials) <= 2500)
    for name, spec in materials.items():
        require(len(name) <= 120 and isinstance(spec["label"], str) and len(spec["label"]) <= 300)
        require(HEX.fullmatch(spec["base_color_hex"]))
        number(spec["roughness"], 0, 1)
        require(spec.get("vein") is None or HEX.fullmatch(spec["vein"]))
        require(spec.get("pattern") in (None, "", "wood", "stone", "weave", "fluted", "terrazzo"))
    parts = scene["parts"]
    models = scene.get("models", [])
    require(isinstance(parts, list) and len(parts) <= 18000)
    require(isinstance(models, list) and len(models) <= 32 and (parts or models))
    for part in parts:
        require(isinstance(part["name"], str) and len(part["name"]) <= 500)
        require(part["material"] in materials and part["shape"] in ("box","round","rounded"))
        vec(part["blender"]["size"], .000001, 20)
        vec(part["blender"]["location"], -500, 500)
        vec(part["rotation_deg"], 0, 270)
        require(part["rotation_deg"][2] in (0, 90, 180, 270))
    for model in models:
        require(isinstance(model["name"], str) and len(model["name"]) <= 150)
        number(model["back"], -500, 500)
        require(isinstance(model["pieces"], list) and 0 < len(model["pieces"]) <= 32)
        for piece in model["pieces"]:
            require(piece["asset"] in MODEL_COUNTS)
            require(type(piece["mesh"]) is int and 0 <= piece["mesh"] < MODEL_COUNTS[piece["asset"]])
            vec(piece["size"], .000001, 20)
            vec(piece["origin"], -500, 500)
            require(piece["finish"] is None or piece["finish"] in materials)
            require(piece["color"] is None or HEX.fullmatch(piece["color"]))
    require("model_assets" not in scene)


@app.function(image=render_image, cpu=4.0, memory=6144, timeout=600, max_containers=2)
def render(scene: dict) -> bytes:
    validate_scene(scene)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source, output = root / "scene.json", root / "render.png"
        source.write_text(json.dumps(scene), encoding="utf-8")
        subprocess.run(["blender", "-b", "-t", "4", "--python-exit-code", "1",
                        "--python", str(RUNTIME / "render_scene.py"), "--",
                        str(source), str(RUNTIME / "assets.json"), str(output)],
                       cwd=folder, timeout=560, check=True)
        return output.read_bytes()


@app.function(image=web_image, secrets=[TOKEN])
@modal.asgi_app()
def web():
    server = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(request):
        key = request.headers.get("X-Access-Key", "").encode()
        expected = os.environ["WOODCRAFT_ACCESS_KEY"].encode()
        if not expected or not hmac.compare_digest(key, expected):
            raise HTTPException(401, "קוד הגישה שגוי.")

    @server.get("/")
    async def index():
        return FileResponse(RUNTIME / "index.html", headers={"Cache-Control": "no-cache"})

    @server.post("/api/render", status_code=202)
    async def request_render(request: Request):
        authenticate(request)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise HTTPException(413, "הפרויקט גדול מדי לרינדור.")
            body.extend(chunk)
        try:
            scene = json.loads(body)
            validate_scene(scene)
        except (ValueError, KeyError, TypeError, OverflowError):
            raise HTTPException(422, "יש נתונים לא תקינים בתכנון. בדוק את מידות החלקים.") from None
        job = await render.spawn.aio(scene)
        signature = hmac.digest(os.environ["WOODCRAFT_ACCESS_KEY"].encode(), job.object_id.encode(), "sha256").hex()
        return {"id": job.object_id + "." + signature}

    @server.get("/api/render/{job_id}")
    async def result(job_id: str, request: Request):
        authenticate(request)
        job, _, signature = job_id.partition(".")
        if not re.fullmatch(r"fc-[A-Za-z0-9]+", job) or not re.fullmatch(r"[a-f0-9]{64}", signature):
            raise HTTPException(404, "הרינדור לא נמצא.")
        expected = hmac.digest(os.environ["WOODCRAFT_ACCESS_KEY"].encode(), job.encode(), "sha256").hex()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(404, "הרינדור לא נמצא.")
        try:
            call = modal.FunctionCall.from_id(job)
            image = await call.get.aio(timeout=0)
        except TimeoutError:
            return Response(status_code=202)
        except Exception:
            raise HTTPException(500, "Blender לא השלים את הרינדור. נסה באיכות טיוטה.") from None
        return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})

    return server
