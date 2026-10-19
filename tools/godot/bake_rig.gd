# Track D rig bake (ADR-0005, ADR-0007). No AI.
#
# Loads a rigged glTF body plus a glTF animation library at runtime, samples one clip into N evenly
# spaced frames, and renders every requested direction through an eye-level orthographic camera:
#   rgba/   grey mannequin, transparent background
#   depth/  near = white, far = black, black background
#   pose/   OpenPose COCO-18 skeleton (ControlNet colours, black background) drawn from TRUE bone
#           positions projected through the same camera, not estimated
# at the cell size and at the gen canvas (cell * scale). Writes bake.json (per-canvas pixels) and
# template.yaml (canvas-independent joints). Needs a real renderer: run through tools/godot/bake.sh.
#
#   godot --path tools/godot --script res://bake_rig.gd -- --rig BODY.gltf --anim-lib LIB.glb \
#     --anim Jog_Fwd_Loop --frames 6,7 --cells 184,176,136 --scale 4 --out DIR
# Other options (defaults): --clip run --fps 10 --dirs north,north-east,east,south-east,south --ppm 62
#   --height-m 1.8 --phase auto --foot-margin 4
extends SceneTree

const DIR_YAW := {"south": 0.0, "south-east": 45.0, "east": 90.0, "north-east": 135.0, "north": 180.0,
	"north-west": 225.0, "west": 270.0, "south-west": 315.0}
# OpenPose COCO-18: nose neck rsho relb rwri lsho lelb lwri rhip rkne rank lhip lkne lank reye leye rear lear
const KP_NAMES := ["nose", "neck", "r_shoulder", "r_elbow", "r_wrist", "l_shoulder", "l_elbow", "l_wrist",
	"r_hip", "r_knee", "r_ankle", "l_hip", "l_knee", "l_ankle", "r_eye", "l_eye", "r_ear", "l_ear"]
# Quaternius / UE-mannequin bone per keypoint ("" = derived: neck = shoulder midpoint, face = head offsets)
const KP_BONES := ["", "", "upperarm_r", "lowerarm_r", "hand_r", "upperarm_l", "lowerarm_l", "hand_l",
	"thigh_r", "calf_r", "foot_r", "thigh_l", "calf_l", "foot_l", "", "", "", ""]
# Face points in the rig's rest pose (model metres, +Z = facing), carried by the head bone.
# Measured from the Quaternius body: head top 1.81 m, eyes mesh at y 1.68-1.71, z <= 0.08.
const FACE_REST := {0: Vector3(0, 1.665, 0.105), 14: Vector3(-0.033, 1.700, 0.082),
	15: Vector3(0.033, 1.700, 0.082), 16: Vector3(-0.078, 1.685, -0.005), 17: Vector3(0.078, 1.685, -0.005)}
const HEAD_CENTER_REST := Vector3(0, 1.700, -0.005)
const FACE_MIN_DOT := {0: -0.25, 14: 0.0, 15: 0.0, 16: -0.3, 17: -0.3}  # visible if outward·to_camera > this
const EXTRA_BONES := ["ball_r", "ball_l", "pelvis", "Head"]  # recorded, not drawn
# ControlNet / DWPose draw_bodypose: limb pairs (0-based) and colours
const LIMBS := [[1, 2], [1, 5], [2, 3], [3, 4], [5, 6], [6, 7], [1, 8], [8, 9], [9, 10], [1, 11], [11, 12],
	[12, 13], [1, 0], [0, 14], [14, 16], [0, 15], [15, 17]]
const COLORS := [[255, 0, 0], [255, 85, 0], [255, 170, 0], [255, 255, 0], [170, 255, 0], [85, 255, 0],
	[0, 255, 0], [0, 255, 85], [0, 255, 170], [0, 255, 255], [0, 170, 255], [0, 85, 255], [0, 0, 255],
	[85, 0, 255], [170, 0, 255], [255, 0, 255], [255, 0, 170], [255, 0, 85]]
const CAM_DIST := 10.0
const DEPTH_HALF_RANGE := 0.45  # metres either side of the rig origin mapped to white..black

var opt := {"anim": "Jog_Fwd_Loop", "frames": "6", "cells": "184", "scale": "4", "dirs":
	"north,north-east,east,south-east,south", "ppm": "62", "height-m": "1.8", "phase": "auto",
	"foot-margin": "4", "fps": "10", "clip": "run", "rig": "", "anim-lib": "", "out": ""}
var body: Node3D
var skel: Skeleton3D
var anim: Animation
var meshes: Array[MeshInstance3D] = []
var mat_grey := StandardMaterial3D.new()
var mat_depth := ShaderMaterial.new()
var world := World3D.new()
var head_idx := -1
var light := DirectionalLight3D.new()


func _initialize() -> void:
	_run.call_deferred()


func _fail(msg: String) -> void:
	printerr("bake_rig: ", msg)
	quit(2)


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var i := 0
	while i < args.size():
		var k: String = args[i].trim_prefix("--")
		if not opt.has(k) or i + 1 >= args.size():
			return _fail("unknown or valueless option: " + args[i])
		opt[k] = args[i + 1]
		i += 2
	for req in ["rig", "anim-lib", "out"]:
		if opt[req] == "":
			return _fail("--%s is required" % req)
	var t0 := Time.get_ticks_msec()
	var err := _load_rig()
	if err != "":
		return _fail(err)
	var ppm := float(opt.ppm)
	var gscale := int(opt.scale)
	var margin := int(opt["foot-margin"])
	var dirs: PackedStringArray = opt.dirs.split(",")
	for d in dirs:
		if not DIR_YAW.has(d):
			return _fail("unknown direction " + d)
	var cells: Array[int] = []
	for c in opt.cells.split(","):
		cells.append(int(c))
	var frame_sets: Array[int] = []
	for f in opt.frames.split(","):
		frame_sets.append(int(f))
	var phase := _auto_phase() if opt.phase == "auto" else float(opt.phase)
	var out_root: String = opt.out
	DirAccess.make_dir_recursive_absolute(out_root)

	# Two viewports per cell share one world: cell size and gen canvas.
	var report := {"rig": opt.rig, "rig_sha256": FileAccess.get_sha256(opt.rig), "anim_lib": opt["anim-lib"],
		"anim_lib_sha256": FileAccess.get_sha256(opt["anim-lib"]), "anim": opt.anim, "clip": opt.clip,
		"fps": int(opt.fps), "anim_length_s": anim.length,
		"phase": phase, "ppm": ppm, "gen_scale": gscale, "height_m": float(opt["height-m"]),
		"directions": dirs, "bakes": []}
	var tmpl := {}  # frames -> dir -> [frame dicts]  (canvas-independent)
	for cell in cells:
		var gen := cell * gscale
		var vp_c := _make_viewport(cell, cell, ppm)
		var vp_g := _make_viewport(gen, cell, ppm)
		for nf in frame_sets:
			# Registration pre-pass (per clip = this frame set): ground the clip's lowest gen-canvas opaque
			# row on (cell - margin) * scale + scale - 1, i.e. cell row cell - margin after the ÷scale
			# downscale. Eye-level ortho: a point's row does not depend on yaw, so south is enough.
			var lowest := -1
			for fi in range(nf):
				_pose_at(fmod(phase + float(fi) / nf, 1.0) * anim.length)
				_aim(vp_g, 0.0, cell, ppm, margin, 0.0)
				var img: Image = await _render(vp_g)
				lowest = max(lowest, _lowest_opaque_row(img))
			var target := (cell - margin) * gscale + gscale - 1
			var off_m := float(target - lowest) / (ppm * gscale)  # raise the camera (lower the feet) by this
			var bake := {"cell": cell, "gen_canvas": gen, "frames": nf, "foot_row_cell": cell - margin,
				"foot_row_gen": target, "register_offset_m": off_m, "lowest_row": {}, "frames_data": {}}
			for d in dirs:
				var yaw: float = DIR_YAW[d]
				bake.lowest_row[d] = {"cell": -1, "gen": -1}
				bake.frames_data[d] = []
				for fi in range(nf):
					var u := fmod(phase + float(fi) / nf, 1.0)
					_pose_at(u * anim.length)
					var fr := {"t": u * anim.length}
					for pair in [[vp_c, 1, "c%d" % cell], [vp_g, gscale, "g%d" % gen]]:
						var vp: SubViewport = pair[0]
						var s: int = pair[1]
						var tag: String = pair[2]
						var cam := _aim(vp, yaw, cell, ppm, margin, off_m)
						var rgba: Image = await _render(vp)
						_set_material(mat_depth)
						var depth: Image = await _render(vp)
						_set_material(mat_grey)
						var kp := _keypoints(cam)
						var size := vp.size.x
						var low := _lowest_opaque_row(rgba)
						var key := "cell" if s == 1 else "gen"
						bake.lowest_row[d][key] = max(bake.lowest_row[d][key], low)
						var base := "%s/f%d/%s" % [out_root, nf, tag]
						_save(rgba, "%s/rgba/%s/%03d.png" % [base, d, fi])
						_save(_on_black(depth), "%s/depth/%s/%03d.png" % [base, d, fi])
						_save(_draw_pose(kp.px, size), "%s/pose/%s/%03d.png" % [base, d, fi])
						fr[key] = {"keypoints": kp.px, "extra": kp.extra_px, "bbox": _rect_list(rgba.get_used_rect())}
						if s == 1:
							fr["joints_m"] = kp.m
							fr["extra_m"] = kp.extra_m
					bake.frames_data[d].append(fr)
			report.bakes.append(bake)
			if not tmpl.has(nf):
				tmpl[nf] = bake
		vp_c.queue_free()
		vp_g.queue_free()
	report["seconds"] = (Time.get_ticks_msec() - t0) / 1000.0
	var f := FileAccess.open(out_root + "/bake.json", FileAccess.WRITE)
	f.store_string(JSON.stringify(report, "  ", false))
	f.close()
	_write_template(out_root + "/template.yaml", report, tmpl, ppm, margin)
	print("bake_rig: done in %.1fs -> %s" % [report.seconds, out_root])
	quit(0)


func _load_rig() -> String:
	var doc := GLTFDocument.new()
	var st := GLTFState.new()
	if doc.append_from_file(opt.rig, st) != OK:
		return "cannot load rig " + opt.rig
	body = doc.generate_scene(st)
	var sks := body.find_children("*", "Skeleton3D", true, false)
	if sks.is_empty():
		return "rig has no Skeleton3D"
	skel = sks[0]
	head_idx = skel.find_bone("Head")
	for n in KP_BONES + EXTRA_BONES:
		if n != "" and skel.find_bone(n) < 0:
			return "rig lacks bone " + n
	var d2 := GLTFDocument.new()
	var s2 := GLTFState.new()
	if d2.append_from_file(opt["anim-lib"], s2) != OK:
		return "cannot load animation library " + opt["anim-lib"]
	var lib := d2.generate_scene(s2)
	var aps := lib.find_children("*", "AnimationPlayer", true, false)
	if aps.is_empty() or not (aps[0] as AnimationPlayer).has_animation(opt.anim):
		return "animation %s not in %s" % [opt.anim, opt["anim-lib"]]
	anim = (aps[0] as AnimationPlayer).get_animation(opt.anim)
	lib.free()
	# Scale so the rest-pose mesh height equals --height-m (density then gives 112 px per 1.8 m).
	var aabb := AABB()
	for m in body.find_children("*", "MeshInstance3D", true, false):
		meshes.append(m)
		aabb = m.get_aabb() if aabb.size == Vector3.ZERO else aabb.merge(m.get_aabb())
	body.scale = Vector3.ONE * (float(opt["height-m"]) / aabb.size.y)
	mat_grey.albedo_color = Color(0.62, 0.62, 0.62)
	mat_grey.roughness = 1.0
	var sh := Shader.new()
	sh.code = """shader_type spatial;
render_mode unshaded, cull_back;
uniform float center = %f; uniform float half_range = %f;
void fragment() { ALBEDO = vec3(clamp((center + half_range + VERTEX.z) / (2.0 * half_range), 0.0, 1.0)); }
""" % [CAM_DIST, DEPTH_HALF_RANGE]
	mat_depth.shader = sh
	_set_material(mat_grey)
	var env := Environment.new()
	env.background_mode = Environment.BG_CLEAR_COLOR
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color(1, 1, 1)
	env.ambient_light_energy = 0.45
	env.tonemap_mode = Environment.TONE_MAPPER_LINEAR
	world.environment = env
	var stage := SubViewport.new()  # holds the rig and the single view-relative light; never rendered
	stage.own_world_3d = false
	stage.world_3d = world
	stage.render_target_update_mode = SubViewport.UPDATE_DISABLED
	stage.add_child(body)
	stage.add_child(light)
	root.add_child(stage)
	return ""


func _set_material(m: Material) -> void:
	for mi in meshes:
		mi.material_override = m


func _pose_at(t: float) -> void:
	skel.reset_bone_poses()
	for tr in anim.get_track_count():
		var bone := skel.find_bone(String(anim.track_get_path(tr).get_concatenated_subnames()))
		if bone < 0:
			continue
		match anim.track_get_type(tr):
			Animation.TYPE_POSITION_3D:
				skel.set_bone_pose_position(bone, anim.position_track_interpolate(tr, t))
			Animation.TYPE_ROTATION_3D:
				skel.set_bone_pose_rotation(bone, anim.rotation_track_interpolate(tr, t))
			Animation.TYPE_SCALE_3D:
				skel.set_bone_pose_scale(bone, anim.scale_track_interpolate(tr, t))


func _bone_world(n: String) -> Vector3:
	return skel.global_transform * skel.get_bone_global_pose(skel.find_bone(n)).origin


# Frame 0 = right-foot touchdown (the clean contact pose Track E seeds from): the first sample after
# the widest right-foot-forward stride where the right foot is within 1 cm of its lowest height.
func _auto_phase() -> float:
	var n := 360
	var stride := PackedFloat32Array()
	var height := PackedFloat32Array()
	for k in n:
		_pose_at(k / float(n) * anim.length)
		var r := skel.get_bone_global_pose(skel.find_bone("foot_r")).origin
		var l := skel.get_bone_global_pose(skel.find_bone("foot_l")).origin
		stride.append(r.z - l.z)
		height.append(r.y)
	var kmax := 0
	for k in n:
		if stride[k] > stride[kmax]:
			kmax = k
	var hmin := height[0]
	for h in height:
		hmin = minf(hmin, h)
	for j in n:
		var k := (kmax + j) % n
		if height[k] <= hmin + 0.01:
			return k / float(n)
	return kmax / float(n)


func _make_viewport(size: int, cell: int, ppm: float) -> SubViewport:
	var vp := SubViewport.new()
	vp.size = Vector2i(size, size)
	vp.transparent_bg = true
	vp.own_world_3d = false
	vp.world_3d = world
	vp.msaa_3d = Viewport.MSAA_DISABLED
	vp.render_target_update_mode = SubViewport.UPDATE_DISABLED
	var cam := Camera3D.new()
	cam.projection = Camera3D.PROJECTION_ORTHOGONAL
	cam.keep_aspect = Camera3D.KEEP_HEIGHT
	cam.size = cell / ppm
	cam.near = 0.05
	cam.far = CAM_DIST * 2.0
	vp.add_child(cam)
	root.add_child(vp)
	return vp


# Eye-level (zero pitch) orthographic camera orbiting the rig; yaw 0 = front (south), 90 = rig faces
# screen-right (east), 180 = back (north). World height off_m lands on the (cell - margin + 1) * s
# row edge, i.e. the bottom edge of the foot row. The one light follows the camera (upper left, front).
func _aim(vp: SubViewport, yaw_deg: float, cell: int, ppm: float, margin: int, off_m: float) -> Camera3D:
	var cam: Camera3D = vp.get_child(0)
	var yc := (cell * 0.5 - (margin - 1)) / ppm + off_m
	var a := deg_to_rad(yaw_deg)
	var pos := Vector3(-CAM_DIST * sin(a), yc, CAM_DIST * cos(a))
	cam.look_at_from_position(pos, Vector3(0, yc, 0), Vector3.UP)
	light.global_transform = Transform3D(cam.global_transform.basis * Basis.from_euler(
		Vector3(deg_to_rad(-35), deg_to_rad(-30), 0)), Vector3.ZERO)
	return cam


func _render(vp: SubViewport) -> Image:
	await process_frame  # flush deferred skeleton/material updates
	vp.render_target_update_mode = SubViewport.UPDATE_ONCE
	await RenderingServer.frame_post_draw
	return vp.get_texture().get_image()


func _keypoints(cam: Camera3D) -> Dictionary:
	var w := {}
	for k in KP_BONES.size():
		if KP_BONES[k] != "":
			w[k] = _bone_world(KP_BONES[k])
	w[1] = (w[2] + w[5]) * 0.5
	var head_xf := skel.global_transform * skel.get_bone_global_pose(head_idx) \
		* skel.get_bone_global_rest(head_idx).affine_inverse()
	var hc := head_xf * HEAD_CENTER_REST
	var to_cam := cam.global_transform.basis.z
	var hidden := {}
	for k in FACE_REST:
		w[k] = head_xf * FACE_REST[k]
		if (w[k] - hc).normalized().dot(to_cam) <= FACE_MIN_DOT[k]:
			hidden[k] = true
	var px := []
	var m := []
	var right := cam.global_transform.basis.x
	for k in KP_NAMES.size():
		var p: Vector3 = w[k]
		var v := cam.unproject_position(p)
		px.append(null if hidden.has(k) else [snappedf(v.x, 0.01), snappedf(v.y, 0.01)])
		m.append(null if hidden.has(k) else [snappedf(p.dot(right), 0.0001), snappedf(p.y, 0.0001)])
	var epx := {}
	var em := {}
	for n in EXTRA_BONES:
		var p := _bone_world(n)
		var v := cam.unproject_position(p)
		epx[n] = [snappedf(v.x, 0.01), snappedf(v.y, 0.01)]
		em[n] = [snappedf(p.dot(right), 0.0001), snappedf(p.y, 0.0001)]
	return {"px": px, "m": m, "extra_px": epx, "extra_m": em}


# Pixel math in code: CPU raster of the ControlNet/DWPose skeleton (filled ellipse limbs at 0.6x
# colour, then full-colour joint discs). Stick/disc size 4 px at 512 px, scaled with the canvas.
func _draw_pose(kp: Array, size: int) -> Image:
	var buf := PackedByteArray()
	buf.resize(size * size * 3)
	buf.fill(0)
	var sw := maxf(1.0, roundf(4.0 * size / 512.0))
	for li in LIMBS.size():
		var a = kp[LIMBS[li][0]]
		var b = kp[LIMBS[li][1]]
		if a == null or b == null:
			continue
		var pa := Vector2(a[0], a[1])
		var pb := Vector2(b[0], b[1])
		var c: Array = COLORS[li]
		_fill_ellipse(buf, size, (pa + pb) * 0.5, pb - pa, sw, [int(c[0] * 0.6), int(c[1] * 0.6), int(c[2] * 0.6)])
	for k in kp.size():
		if kp[k] != null:
			_fill_ellipse(buf, size, Vector2(kp[k][0], kp[k][1]), Vector2.ZERO, sw, COLORS[k])
	return Image.create_from_data(size, size, false, Image.FORMAT_RGB8, buf)


func _fill_ellipse(buf: PackedByteArray, size: int, c: Vector2, axis: Vector2, minor: float, col: Array) -> void:
	var major := maxf(axis.length() * 0.5, minor)
	var u := axis.normalized() if axis.length() > 0.0 else Vector2.RIGHT
	var r := ceili(major) + 1
	for y in range(maxi(0, floori(c.y) - r), mini(size, floori(c.y) + r + 1)):
		for x in range(maxi(0, floori(c.x) - r), mini(size, floori(c.x) + r + 1)):
			var d := Vector2(x + 0.5, y + 0.5) - c
			var a := d.dot(u) / major
			var b := d.dot(Vector2(-u.y, u.x)) / minor
			if a * a + b * b <= 1.0:
				var o := (y * size + x) * 3
				buf[o] = col[0]
				buf[o + 1] = col[1]
				buf[o + 2] = col[2]


func _lowest_opaque_row(img: Image) -> int:
	var data := img.get_data()  # RGBA8
	var w := img.get_width()
	for y in range(img.get_height() - 1, -1, -1):
		var o := y * w * 4 + 3
		for x in w:
			if data[o + x * 4] >= 128:
				return y
	return -1


func _on_black(img: Image) -> Image:
	var bg := Image.create(img.get_width(), img.get_height(), false, Image.FORMAT_RGBA8)
	bg.fill(Color(0, 0, 0, 1))
	bg.blend_rect(img, Rect2i(Vector2i.ZERO, img.get_size()), Vector2i.ZERO)
	bg.convert(Image.FORMAT_L8)
	return bg


func _rect_list(r: Rect2i) -> Array:
	return [r.position.x, r.position.y, r.size.x, r.size.y]


func _save(img: Image, path: String) -> void:
	DirAccess.make_dir_recursive_absolute(path.get_base_dir())
	img.save_png(path)


# Canvas-independent template: joints as (x, h) in cell pixels at --ppm, x right of anchor_x
# (cell / 2), h above the foot line's bottom edge. Any canvas: px = W/2 + x*s, py = (cell-3)*s - h*s.
func _write_template(path: String, report: Dictionary, tmpl: Dictionary, ppm: float, margin: int) -> void:
	var L := PackedStringArray()
	L.append("# --- generated by tools/godot/bake_rig.gd; do not edit below this line ---")
	L.append("clip: {name: %s, fps: %d, loop: true, source_anim: %s}" % [report.clip, report.fps, report.anim])
	L.append("directions: [%s]   # west side mirrored from east (ADR-0007)" % ", ".join(report.directions))
	L.append("source: {rig: %s, rig_sha256: %s, anim_lib: %s, anim_lib_sha256: %s, height_m: %s}" % [
		report.rig.get_file(), report.rig_sha256, report.anim_lib.get_file(), report.anim_lib_sha256, report.height_m])
	L.append("units: {x: cell_px_right_of_anchor_x, h: cell_px_above_foot_edge, ppm: %s}" % ppm)
	L.append("anchor: {x: cell_half, foot_row: cell_minus_%d, mode: per_clip}   # lowest opaque row of the clip" % margin)
	L.append("to_canvas: \"px = W/2 + x*s ; py = (cell - %d)*s - h*s ; s = W/cell\"" % (margin - 1))
	L.append("keypoint_order: [%s]   # OpenPose COCO-18; null = hidden face point" % ", ".join(KP_NAMES))
	L.append("extra_order: [%s]   # foot = lower of ball_r/ball_l (rig foot anchor)" % ", ".join(EXTRA_BONES))
	L.append("phase: %s   # clip fraction at frame 0 (auto = right-foot touchdown)" % snappedf(report.phase, 0.0001))
	L.append("anim_length_s: %s" % snappedf(report.anim_length_s, 0.0001))
	L.append("frame_sets:")
	for nf in tmpl:
		var bake: Dictionary = tmpl[nf]
		var ground: float = bake.register_offset_m
		L.append("  %d:" % nf)
		for d in bake.frames_data:
			L.append("    %s:" % d)
			for fi in bake.frames_data[d].size():
				var fr: Dictionary = bake.frames_data[d][fi]
				var js := PackedStringArray()
				for j in fr.joints_m:
					js.append("null" if j == null else "[%s, %s]" % [snappedf(j[0] * ppm, 0.01), snappedf((j[1] - ground) * ppm, 0.01)])
				var ex := PackedStringArray()
				for n in EXTRA_BONES:
					var j: Array = fr.extra_m[n]
					ex.append("[%s, %s]" % [snappedf(j[0] * ppm, 0.01), snappedf((j[1] - ground) * ppm, 0.01)])
				var foot: Array = fr.extra_m.ball_r if fr.extra_m.ball_r[1] <= fr.extra_m.ball_l[1] else fr.extra_m.ball_l
				var fs := "[%s, %s]" % [snappedf(foot[0] * ppm, 0.01), snappedf((foot[1] - ground) * ppm, 0.01)]
				L.append("      - {t: %s, foot: %s, kp: [%s], extra: [%s]}" % [snappedf(fr.t, 0.0001), fs, ", ".join(js), ", ".join(ex)])
	var f := FileAccess.open(path, FileAccess.WRITE)
	f.store_string("\n".join(L) + "\n")
	f.close()
