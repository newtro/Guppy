"""Add talking shape keys to the normalized Tripo head (face = +X, up = +Z) and export GLB.
blender -b -P rig_shapes.py -- <guppy_raw.blend>
"""
import bpy, sys, math
from pathlib import Path
from mathutils import Vector, Matrix

blend = Path(sys.argv[sys.argv.index("--")+1]).resolve()
bpy.ops.wm.open_mainfile(filepath=str(blend))
o = [x for x in bpy.data.objects if x.type == "MESH"][0]
# bake object transform into mesh so shape-key math is in world units
bpy.context.view_layer.objects.active = o
for x in bpy.data.objects: x.select_set(x == o)
if o.parent:
    mw = o.matrix_world.copy(); o.parent = None; o.matrix_world = mw
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

MOUTH_Z, CORNER_Y, SNOUT_X = 0.600, 0.10, 0.48
HINGE = Vector((0.10, 0.0, 0.605))

def ss(e0, e1, x):  # smoothstep
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0))); return t * t * (3 - 2 * t)

co = [v.co.copy() for v in o.data.vertices]
# jaw weight: below the mouth line, forward of the throat, above the collar
jaw = [ss(MOUTH_Z, MOUTH_Z - 0.02, c.z) * ss(-0.02, 0.20, c.x) * ss(0.39, 0.46, c.z) for c in co]
# lip-region weight: near the mouth opening
lip = [ss(0.07, 0.0, abs(c.z - MOUTH_Z)) * ss(0.25, 0.40, c.x) * ss(CORNER_Y + 0.06, CORNER_Y - 0.02, abs(c.y)) for c in co]
corner = [ss(0.05, 0.0, abs(c.z - MOUTH_Z)) * ss(0.25, 0.38, c.x) * ss(0.03, CORNER_Y, abs(c.y)) * ss(CORNER_Y + 0.08, CORNER_Y, abs(c.y)) for c in co]

o.shape_key_add(name="Basis")
def key(name, fn):
    k = o.shape_key_add(name=name, from_mix=False)
    for i, c in enumerate(co):
        k.data[i].co = fn(i, c)

def rot_jaw(deg):
    R = Matrix.Rotation(math.radians(deg), 3, "Y")  # +Y rotation tips +X side downward
    return lambda i, c: c if jaw[i] == 0 else c.lerp(HINGE + R @ (c - HINGE), jaw[i])

key("jawOpen", rot_jaw(14))
key("mouthWide", lambda i, c: c + Vector((-0.01 * corner[i], math.copysign(0.025, c.y) * corner[i], 0)))
key("mouthRound", lambda i, c: c + Vector((0.015 * lip[i], -c.y * 0.35 * lip[i], 0)))
key("mouthSmile", lambda i, c: c + Vector((-0.01 * corner[i], math.copysign(0.01, c.y) * corner[i], 0.02 * corner[i])))
key("mouthFrown", lambda i, c: c + Vector((0, 0, -0.02 * corner[i])))
key("mouthPress", lambda i, c: c + Vector((0.005 * lip[i], 0, (MOUTH_Z - c.z) * 0.4 * lip[i])))

out = blend.parent
bpy.ops.wm.save_as_mainfile(filepath=str(out / "guppy_rigged.blend"))

# review renders: face-on camera at +X
scene = bpy.context.scene; cam = scene.camera
target = Vector((0, 0, 0.60))
for label, az in [("face", 0), ("face34", 35)]:
    a = math.radians(az)
    cam.location = target + Vector((math.cos(a) * 2.0, math.sin(a) * 2.0, 0.05))
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    for kb in o.data.shape_keys.key_blocks[1:]:
        for other in o.data.shape_keys.key_blocks[1:]: other.value = 0
        kb.value = 1.0
        scene.render.filepath = str(out / "keys" / f"{label}_{kb.name}.png")
        bpy.ops.render.render(write_still=True)
    for other in o.data.shape_keys.key_blocks[1:]: other.value = 0
    scene.render.filepath = str(out / "keys" / f"{label}_neutral.png")
    bpy.ops.render.render(write_still=True)

bpy.ops.export_scene.gltf(filepath=str(out / "guppy_head.glb"), export_morph=True, export_apply=False)
print("DONE", sum(1 for w in jaw if w > 0), "jaw verts")
