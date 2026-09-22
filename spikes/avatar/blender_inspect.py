"""Import a Tripo GLB, normalize it, save a .blend and render review views.

blender -b -P blender_inspect.py -- out/<task>.glb
"""
import math, sys
from pathlib import Path
import bpy
from mathutils import Vector

src = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
out_dir = src.parent / src.stem
out_dir.mkdir(exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
if src.suffix.lower() == ".fbx":
    bpy.ops.import_scene.fbx(filepath=str(src))
else:
    bpy.ops.import_scene.gltf(filepath=str(src))
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]

# Bounds across all meshes -> center on origin, scale to 1m tall, sit on z=0
pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
size = hi - lo
roots = [o for o in bpy.context.scene.objects if o.parent is None]
s = 1.0 / size.z
for r in roots:
    r.location = (r.location - Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))) * s
    r.scale = r.scale * s
bpy.context.view_layer.update()

stats = {o.name: (len(o.data.vertices), len(o.data.polygons), len(o.data.materials)) for o in meshes}
print("MESHES", stats, "SIZE", tuple(round(v, 3) for v in size))

# Lighting + camera
scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items} else "BLENDER_EEVEE"
scene.render.resolution_x = scene.render.resolution_y = 768
world = bpy.data.worlds.new("w"); scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[1].default_value = 0.6
for name, rot, energy in [("key", (50, 0, 35), 4), ("fill", (60, 0, -60), 1.5), ("rim", (120, 0, 180), 3)]:
    L = bpy.data.lights.new(name, "SUN"); L.energy = energy
    o = bpy.data.objects.new(name, L); o.rotation_euler = [math.radians(a) for a in rot]
    scene.collection.objects.link(o)

cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
cam.data.lens = 85
scene.collection.objects.link(cam); scene.camera = cam
target = Vector((0, 0, 0.62))
for label, az in [("front", 0), ("three_quarter", 35), ("side", 90)]:
    a = math.radians(az)
    # Tripo models face +X after import
    cam.location = target + Vector((math.cos(a) * 3.2, math.sin(a) * 3.2, 0.1))
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(out_dir / f"{label}.png")
    bpy.ops.render.render(write_still=True)

bpy.ops.wm.save_as_mainfile(filepath=str(out_dir / "guppy_raw.blend"))
print("RENDERED", out_dir)
