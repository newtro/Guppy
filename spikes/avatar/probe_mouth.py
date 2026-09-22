import bpy, sys
from mathutils import Vector
from mathutils.bvhtree import BVHTree
blend = sys.argv[sys.argv.index("--")+1]
bpy.ops.wm.open_mainfile(filepath=blend)
o = [x for x in bpy.data.objects if x.type=="MESH"][0]
dg = bpy.context.evaluated_depsgraph_get()
bvh = BVHTree.FromObject(o, dg)
mw = o.matrix_world; inv = mw.inverted()
# face points +X. Raycast from +X toward -X over a y/z grid; print hit x depth.
ys = [i*0.02 for i in range(-10,11)]
zs = [0.56 - i*0.01 for i in range(0,32)]
print("     " + "".join(f"{y:+.2f}"[1:4].rjust(4) for y in ys))
for z in zs:
    row=[]
    for y in ys:
        org = inv @ Vector((2,y,z)); d = (inv.to_3x3() @ Vector((-1,0,0))).normalized()
        hit,_,_,_ = bvh.ray_cast(org,d)
        row.append(f"{(mw@hit).x:4.2f}"[1:] if hit else "  . ")
    print(f"{z:.2f} " + "".join(r.rjust(4) for r in row))
