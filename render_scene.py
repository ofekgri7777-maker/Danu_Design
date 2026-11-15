import bpy
import bmesh
import math
from mathutils import Vector

import json
import sys
from pathlib import Path

input_path, assets_path, output_path = sys.argv[sys.argv.index("--") + 1:]
DATA = json.loads(Path(input_path).read_text())
ASSETS = json.loads(Path(assets_path).read_text())
PROJECT = DATA["project"]
STUDIO = DATA["studio"]
SAMPLES = DATA["samples"]
RESOLUTION = (1280, 720) if SAMPLES <= 32 else (1920, 1080)
bounds = DATA["dims_mm"]
DIMS = tuple(bounds[axis] / 1000 for axis in ("W","D","H"))
WALL_Y = max(
    [row["blender"]["location"][1] + row["blender"]["size"][0 if row["rotation_deg"][2] % 180 else 1] / 2
     for row in DATA["parts"]] + [model["back"] for model in DATA.get("models", [])] + [0]
) + (1.2 if DATA["template"] == "island" else .003)
MATERIALS = {
    k: {"label":m["label"], "hex":m["base_color_hex"], "roughness":m["roughness"],
        "wood":m.get("texture") == "wood_grain", "grain":m.get("grain"),
        "vein":m.get("vein"), "pattern":m.get("pattern")}
    for k,m in DATA["materials"].items()
}
PARTS = [(p["name"],p["material"],p["blender"]["size"],p["blender"]["location"],p["shape"],p["rotation_deg"][2])
         for p in DATA["parts"]]

def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_rgba(h, mult=1.0):
    h = h.lstrip("#")
    return tuple(min(1.0, srgb_to_linear(int(h[i:i + 2], 16) / 255) * mult) for i in (0, 2, 4)) + (1.0,)


scene = bpy.data.scenes.new("WoodCraft - " + PROJECT)
if bpy.context.window:
    bpy.context.window.scene = scene
col = bpy.data.collections.new("WoodCraft")
scene.collection.children.link(col)


def make_material(key, spec):
    m = bpy.data.materials.new("WC_" + key)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = hex_rgba(spec["hex"])
    bsdf.inputs["Roughness"].default_value = spec["roughness"]
    if spec["vein"]:
        coord = nt.nodes.new("ShaderNodeTexCoord")
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.wave_type = "BANDS"
        wave.bands_direction = "DIAGONAL"
        wave.inputs["Scale"].default_value = 2.5
        wave.inputs["Distortion"].default_value = 4.0
        wave.inputs["Detail"].default_value = 3
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].position = 0.02
        ramp.color_ramp.elements[0].color = hex_rgba(spec["vein"])
        ramp.color_ramp.elements[1].position = 0.17
        ramp.color_ramp.elements[1].color = hex_rgba(spec["hex"])
        nt.links.new(coord.outputs["Object"], wave.inputs["Vector"])
        nt.links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    elif spec["pattern"] in ("stone", "weave", "fluted", "terrazzo"):
        coord = nt.nodes.new("ShaderNodeTexCoord")
        pattern = spec["pattern"]
        if pattern in ("weave", "fluted"):
            tex = nt.nodes.new("ShaderNodeTexWave")
            tex.wave_type = "RINGS" if pattern == "weave" else "BANDS"
            tex.bands_direction = "X"
            tex.inputs["Scale"].default_value = 58 if pattern == "weave" else 17
            channel = tex.outputs["Fac"]
        else:
            tex = nt.nodes.new("ShaderNodeTexNoise" if pattern == "stone" else "ShaderNodeTexVoronoi")
            tex.inputs["Scale"].default_value = 32 if pattern == "stone" else 23
            channel = tex.outputs["Fac"] if pattern == "stone" else tex.outputs["Distance"]
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].position = .18
        ramp.color_ramp.elements[0].color = hex_rgba(spec["hex"], .61)
        ramp.color_ramp.elements[1].position = .61
        ramp.color_ramp.elements[1].color = hex_rgba(spec["hex"])
        nt.links.new(coord.outputs["Generated"], tex.inputs["Vector"])
        nt.links.new(channel, ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    elif spec["wood"]:
        vertical = spec["grain"] == "vertical"
        coord = nt.nodes.new("ShaderNodeTexCoord")
        mapping = nt.nodes.new("ShaderNodeMapping")
        mapping.inputs["Scale"].default_value = (1.0, 1.0, 0.12) if vertical else (0.12, 1.0, 1.0)
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.wave_type = "BANDS"
        wave.bands_direction = "X" if vertical else "Z"
        wave.inputs["Scale"].default_value = 5.0
        wave.inputs["Distortion"].default_value = 10.0
        wave.inputs["Detail"].default_value = 10.0
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = hex_rgba(spec["hex"], 0.62)
        ramp.color_ramp.elements[1].color = hex_rgba(spec["hex"], 1.0)
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.05
        nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
        nt.links.new(mapping.outputs["Vector"], wave.inputs["Vector"])
        nt.links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
        nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(wave.outputs["Fac"], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m


MATS = {k: make_material(k, v) for k, v in MATERIALS.items()}


def add_box(name, mat, size, loc, bevel=0.001, shape="box"):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    if shape == "round":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=0.5, radius2=0.5, depth=1.0)
        for vertex in bm.verts:
            vertex.co.y, vertex.co.z = -vertex.co.z, vertex.co.y
    elif shape == "rounded":
        bmesh.ops.create_uvsphere(bm, u_segments=18, v_segments=12, radius=0.5)
    else:
        bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    col.objects.link(ob)
    if bevel:
        mod = ob.modifiers.new("Bevel 1mm", "BEVEL")
        mod.width = bevel
        mod.segments = 2
        mod.limit_method = "ANGLE"
    return ob


for name, mat_key, size, loc, shape, yaw in PARTS:
    obj = add_box(name, MATS[mat_key], size, loc, bevel=min(0.001, min(size)/5) if shape == "box" else 0, shape=shape)
    obj.rotation_euler.z = math.radians(yaw)


import base64
import json
import tempfile
from pathlib import Path

model_assets = ASSETS
models = DATA.get("models", [])
appearance = {}
with tempfile.TemporaryDirectory() as folder:
    for key, asset in model_assets.items():
        for index, data in enumerate(asset["textures"]):
            is_color = data.startswith("data:image/jpeg")
            path = Path(folder) / (key + "-" + str(index) + (".jpg" if is_color else ".png"))
            path.write_bytes(base64.b64decode(data.split(",", 1)[1]))
            image = bpy.data.images.load(str(path))
            if not is_color:
                image.colorspace_settings.name = "Non-Color"
            image.pack()
            appearance[(key, index)] = image

shapr_materials = {}

def shapr_material(asset_key, piece, data):
    hardware = piece["role"] in ("plinth", "handle")
    if not hardware and data["finish"] and piece["role"] != "oven":
        return MATS[data["finish"]]
    key = (asset_key, tuple(piece["maps"]), data["color"] if hardware else piece.get("color"), piece.get("roughness"), piece.get("metalness"))
    if key in shapr_materials:
        return shapr_materials[key]
    material = bpy.data.materials.new("Shapr3D " + piece["role"])
    material.use_nodes = True
    tree = material.node_tree
    bsdf = tree.nodes["Principled BSDF"]

    def tex(index):
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = appearance[(asset_key, index)]
        return node

    if hardware:
        normal_map, surface_map, coat_map = piece["maps"]
        bsdf.inputs["Base Color"].default_value = hex_rgba(data["color"]) if data["color"] else (0.005684, 0.005684, 0.005684, 1)
        surface = tree.nodes.new("ShaderNodeSeparateColor")
        tree.links.new(tex(surface_map).outputs["Color"], surface.inputs[0])
        tree.links.new(surface.outputs["Green"], bsdf.inputs["Roughness"])
        tree.links.new(surface.outputs["Blue"], bsdf.inputs["Metallic"])
        normal = tree.nodes.new("ShaderNodeNormalMap")
        tree.links.new(tex(normal_map).outputs["Color"], normal.inputs["Color"])
        tree.links.new(normal.outputs["Normal"], bsdf.inputs["Normal"])
        if "Coat Weight" in bsdf.inputs:
            bsdf.inputs["Coat Weight"].default_value = 0.12
            coat = tree.nodes.new("ShaderNodeSeparateColor")
            tree.links.new(tex(coat_map).outputs["Color"], coat.inputs[0])
            tree.links.new(coat.outputs["Green"], bsdf.inputs["Coat Roughness"])
    elif piece.get("color"):
        bsdf.inputs["Base Color"].default_value = hex_rgba(piece["color"])
        bsdf.inputs["Roughness"].default_value = piece["roughness"]
        bsdf.inputs["Metallic"].default_value = piece["metalness"]
    else:
        tree.links.new(tex(piece["maps"][0]).outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.5
    shapr_materials[key] = material
    return material

for model in models:
    for data in model["pieces"]:
        key = data["asset"]
        piece = model_assets[key]["meshes"][data["mesh"]]
        width, height, depth = data["size"]
        source = piece["position"]
        vertices = [(source[i] * width, -source[i+2] * depth, source[i+1] * height)
                    for i in range(0, len(source), 3)]
        indices = piece["indices"]
        faces = [indices[i:i+3] for i in range(0, len(indices), 3)]
        mesh = bpy.data.meshes.new(model["name"])
        mesh.from_pydata(vertices, [], faces)
        mesh.update()
        uv = mesh.uv_layers.new()
        for loop in mesh.loops:
            u, v = piece["uv"][loop.vertex_index*2:loop.vertex_index*2+2]
            uv.data[loop.index].uv = (u, 1 - v)
        mesh.materials.append(shapr_material(key, piece, data))
        obj = bpy.data.objects.new(model["name"] + " · " + piece["role"], mesh)
        obj.location = data["origin"]
        col.objects.link(obj)

# ------------------------------------------------------------ studio
ENV = {
    "studio":  {"floor": "#E7E5E1", "wall": "#E7E5E1", "floor_rough": 0.8},
    "room":    {"floor": "#A7825C", "wall": "#ECE9E4", "floor_rough": 0.45},
    "kitchen": {"floor": "#BDB9B2", "wall": "#E3E0DA", "floor_rough": 0.3},
}[STUDIO]


def flat_material(name, hexc, rough):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = hex_rgba(hexc)
    b.inputs["Roughness"].default_value = rough
    return m


add_box("Floor", flat_material("WC_Floor", ENV["floor"], ENV["floor_rough"]), (24, 24, 0.02), (0, 0, -0.01), bevel=0)
add_box("Wall", flat_material("WC_Wall", ENV["wall"], 0.9), (24, 0.1, 8), (0, WALL_Y + 0.05, 4), bevel=0)

# ------------------------------------------------------------ camera (35 mm) + three-point light
w, d, h = DIMS
center = Vector((0, -d / 2, h * 0.5))
hfov = 2 * math.atan(18 / 35)
dist = max(w * 1.25, h * 2.0) / (2 * math.tan(hfov / 2)) + d
cam_data = bpy.data.cameras.new("WC_Camera")
cam_data.lens = 35
cam = bpy.data.objects.new("WC_Camera", cam_data)
cam.location = center + Vector((dist * 0.45, -dist * 0.9, h * 0.25 + 0.35))
cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
col.objects.link(cam)
scene.camera = cam


def area_light(name, offset, size, power):
    ld = bpy.data.lights.new(name, "AREA")
    ld.size = size
    ld.energy = power
    ob = bpy.data.objects.new(name, ld)
    ob.location = center + Vector(offset)
    ob.rotation_euler = (center - ob.location).to_track_quat("-Z", "Y").to_euler()
    col.objects.link(ob)


s = max(w, h, 1.0)
area_light("Key", (-s * 1.2, -s * 1.4, s * 1.3), s * 1.2, 90 * s * s)
area_light("Fill", (s * 1.6, -s * 1.0, s * 0.5), s * 1.5, 25 * s * s)
area_light("Rim", (s * 0.4, s * 0.8 - d, s * 1.6), s * 0.8, 45 * s * s)

world = bpy.data.worlds.new("WC_World")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[1].default_value = 0.12
scene.world = world

# ------------------------------------------------------------ render
scene.render.engine = "CYCLES"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = False
scene.render.resolution_x, scene.render.resolution_y = RESOLUTION
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dev in ("OPTIX", "CUDA", "HIP", "METAL"):
        try:
            prefs.compute_device_type = dev
            prefs.get_devices()
            if any(x.type == dev for x in prefs.devices):
                for x in prefs.devices:
                    x.use = x.type == dev
                scene.cycles.device = "GPU"
                break
        except TypeError:
            continue
except Exception as e:
    print("GPU not available, rendering on CPU:", e)
for vt in ("AgX", "Filmic"):
    try:
        scene.view_settings.view_transform = vt
        break
    except TypeError:
        continue

print("WoodCraft scene ready:", len(PARTS), "parts. Press F12 to render.")

scene.render.image_settings.file_format = "PNG"
scene.render.filepath = output_path
bpy.ops.render.render(write_still=True, scene=scene.name)
