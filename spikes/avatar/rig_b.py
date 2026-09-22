"""Rig model B (Mon-Cal style head, face = +X, up = +Z): mouth morphs + eyelid meshes with blink morphs.
blender -b -P rig_b.py -- <guppy_raw.blend>
Landmarks come from probe_mouth.py.
"""
import bpy, bmesh, sys, math, colorsys
from pathlib import Path
from mathutils import Vector, Matrix

blend = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
out = blend.parent
bpy.ops.wm.open_mainfile(filepath=str(blend))
head = [x for x in bpy.data.objects if x.type == "MESH"][0]
head.name = "GuppyHead"
bpy.context.view_layer.objects.active = head
for x in bpy.data.objects: x.select_set(x == head)
if head.parent:
    mw = head.matrix_world.copy(); head.parent = None; head.matrix_world = mw
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# --- landmarks (world units after normalization) ---
MOUTH_Z, CORNER_Y = 0.455, 0.05
FACE_X0, FACE_X1 = 0.40, 0.46      # fade: throat/collar (<x0) .. face front (>x1)
JAW_DROP = 0.045

def ss(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0))); return t * t * (3 - 2 * t)

co = [v.co.copy() for v in head.data.vertices]
front = [ss(FACE_X0, FACE_X1, c.x) for c in co]
jaw = [ss(MOUTH_Z + 0.005, MOUTH_Z - 0.02, c.z) * front[i] * ss(0.22, 0.15, abs(c.y)) for i, c in enumerate(co)]
lip = [ss(0.05, 0.0, abs(c.z - MOUTH_Z)) * front[i] * ss(CORNER_Y + 0.05, CORNER_Y, abs(c.y)) for i, c in enumerate(co)]
corner = [ss(0.04, 0.0, abs(c.z - MOUTH_Z)) * front[i] * ss(0.01, CORNER_Y, abs(c.y)) * ss(CORNER_Y + 0.06, CORNER_Y, abs(c.y)) for i, c in enumerate(co)]

head.shape_key_add(name="Basis")
def key(obj, name, pts, fn):
    k = obj.shape_key_add(name=name, from_mix=False)
    for i, c in enumerate(pts): k.data[i].co = fn(i, c)

# jaw: drop + slight back tilt, taking the barbels with it
key(head, "jawOpen", co, lambda i, c: c + Vector((-0.012, 0, -JAW_DROP)) * jaw[i])
key(head, "mouthWide", co, lambda i, c: c + Vector((-0.01, math.copysign(0.03, c.y), 0.004)) * corner[i])
key(head, "mouthRound", co, lambda i, c: c + Vector((0.025 * lip[i], -c.y * 0.5 * lip[i], 0)))
key(head, "mouthSmile", co, lambda i, c: c + Vector((-0.012, math.copysign(0.015, c.y), 0.03)) * corner[i])
key(head, "mouthFrown", co, lambda i, c: c + Vector((0, math.copysign(0.005, c.y), -0.03)) * corner[i])
key(head, "mouthPress", co, lambda i, c: c + Vector((0.01 * lip[i], 0, (MOUTH_Z - c.z) * 0.7 * lip[i])))

# --- eyes: find iris verts by texture colour ---
mat = head.active_material
img = next(n.image for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image)
W, H = img.size; px = img.pixels[:]
uv = head.data.uv_layers.active.data
vert_uv = {}
for poly in head.data.polygons:
    for li in poly.loop_indices:
        vert_uv.setdefault(head.data.loops[li].vertex_index, uv[li].uv)

def color(vi):
    u, v = vert_uv[vi]; x = min(W - 1, int((u % 1) * W)); y = min(H - 1, int((v % 1) * H)); o = (y * W + x) * 4
    return px[o:o + 3]

iris = {1: [], -1: []}
nrm = [v.normal.copy() for v in head.data.vertices]
for vi, c in enumerate(co):
    if vi not in vert_uv or c.z < 0.55 or abs(c.y) < 0.08: continue
    h, s, v = colorsys.rgb_to_hsv(*color(vi))
    if 0.05 < h < 0.13 and s > 0.6 and v > 0.45:
        iris[1 if c.y > 0 else -1].append(vi)

def median(vals):
    vals = sorted(vals); return vals[len(vals) // 2]

skin = Vector((0, 0, 0)); n_skin = 0
lids = []
left_mean = None
for side in (1, -1):
    idx = iris[side]
    if left_mean is None:
        # keep the main blob: within 0.09 of the per-axis median
        m = Vector((median([co[i].x for i in idx]), median([co[i].y for i in idx]), median([co[i].z for i in idx])))
    else:
        m = Vector((left_mean.x, -left_mean.y, left_mean.z))  # head is near-symmetric: search the mirror
    idx = [i for i in idx if (co[i] - m).length < 0.09]
    mean = sum((co[i] for i in idx), Vector()) / len(idx)
    if left_mean is None: left_mean = mean
    fwd = sum((nrm[i] for i in idx), Vector()).normalized()
    dists = sorted(((co[i] - mean) - fwd * (co[i] - mean).dot(fwd)).length for i in idx)
    r_iris = dists[int(len(dists) * 0.9)]
    r = r_iris * 1.1
    c = mean - fwd * r * 0.55
    print("EYE", side, len(idx), "verts r_iris", round(r_iris, 3))
    for vi, p in enumerate(co):
        d = (p - mean).length
        if r_iris * 1.1 < d < r_iris * 1.8 and p.z > mean.z + r_iris * 0.8 and vi in vert_uv:  # brow skin
            h, s_, v = colorsys.rgb_to_hsv(*color(vi))
            if not (0.05 < h < 0.13 and s_ > 0.6): skin += Vector(color(vi)); n_skin += 1
    lids.append((side, c, r, fwd))
    print("  center", tuple(round(x, 3) for x in c), "r", round(r, 3), "fwd", tuple(round(x, 2) for x in fwd))

skin_rgb = skin / max(1, n_skin)
if img.colorspace_settings.name == "sRGB":  # pixels are display-encoded; Base Color wants linear
    skin_rgb = Vector([((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in skin_rgb])
print("LID RGB", tuple(round(c, 3) for c in skin_rgb))
lid_mat = bpy.data.materials.new("GuppyLid"); lid_mat.use_nodes = True
bsdf = lid_mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (*skin_rgb, 1); bsdf.inputs["Roughness"].default_value = 0.35

SPAN_U, TOP, BOTTOM, NU, NV = math.radians(80), math.radians(75), math.radians(-70), 24, 16
for side, c, r, fwd in lids:
    up0 = Vector((0, 0, 1)); right = fwd.cross(up0).normalized(); up = right.cross(fwd).normalized()
    R = r * 1.08
    def sph(a, e):  # a = horizontal angle, e = elevation, around fwd
        d = (fwd * math.cos(e) * math.cos(a) + right * math.cos(e) * math.sin(a) + up * math.sin(e))
        return c + d * R
    closed = [[sph(-SPAN_U + 2 * SPAN_U * i / NU, TOP + (BOTTOM - TOP) * j / NV) for i in range(NU + 1)] for j in range(NV + 1)]
    opened = [[sph(-SPAN_U + 2 * SPAN_U * i / NU, TOP) for i in range(NU + 1)] for j in range(NV + 1)]
    me = bpy.data.meshes.new(f"lid_{side}")
    verts = [p for row in opened for p in row]
    faces = [(j * (NU + 1) + i, j * (NU + 1) + i + 1, (j + 1) * (NU + 1) + i + 1, (j + 1) * (NU + 1) + i) for j in range(NV) for i in range(NU)]
    me.from_pydata(verts, [], faces); me.materials.append(lid_mat)
    ob = bpy.data.objects.new("GuppyLidL" if side > 0 else "GuppyLidR", me); bpy.context.scene.collection.objects.link(ob)
    for p in me.polygons: p.use_smooth = True
    ob.shape_key_add(name="Basis")
    flat = [p for row in closed for p in row]
    key(ob, "eyeBlink", flat, lambda i, p: p)

# --- review renders ---
scene = bpy.context.scene; cam = scene.camera
target = Vector((0, 0, 0.55))
def set_all(name, val):
    for ob in bpy.data.objects:
        if ob.type == "MESH" and ob.data.shape_keys:
            for kb in ob.data.shape_keys.key_blocks[1:]:
                kb.value = val if kb.name == name else 0.0
states = ["neutral", "jawOpen", "mouthWide", "mouthRound", "mouthSmile", "mouthFrown", "mouthPress", "eyeBlink"]
(out / "keys_b").mkdir(exist_ok=True)
for label, az, dist in [("face", 0, 2.4), ("face34", 35, 2.4)]:
    a = math.radians(az)
    cam.location = target + Vector((math.cos(a) * dist, math.sin(a) * dist, 0.05))
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    for st in states:
        set_all(st, 1.0)
        scene.render.filepath = str(out / "keys_b" / f"{label}_{st}.png")
        bpy.ops.render.render(write_still=True)
set_all("", 0)

bpy.ops.wm.save_as_mainfile(filepath=str(out / "guppy_rigged.blend"))
for x in bpy.data.objects: x.select_set(x.type == "MESH")
bpy.ops.export_scene.gltf(filepath=str(out / "guppy_head.glb"), use_selection=True, export_morph=True)
print("DONE jaw verts", sum(1 for w in jaw if w > 0.01))
