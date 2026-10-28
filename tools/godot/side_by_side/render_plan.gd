# Draws the quads listed in plan.json, one SubViewport per render, and saves each as a transparent-background PNG
# (the tool composites the background in Python so it is exact). Mirrors how PixelStandee.cs drives its shader:
# a QuadMesh of cell / px_per_m metres, MaterialOverride = ShaderMaterial, uniforms sheet / grid / cell / cell_pixels
# only. Orthographic camera facing the quads, so 1 sheet pixel = `scale` render pixels exactly.
#
#   godot --path PROJECT --script res://render_plan.gd -- --plan res://plan.json --out /abs/raw/dir
# Prints one JSON line. Exit 0 = every planned render was saved within the byte budget.
extends SceneTree

func _initialize() -> void:
	var a := OS.get_cmdline_user_args()
	var opt := {}
	for i in range(0, a.size() - 1, 2):
		opt[a[i].trim_prefix("--")] = a[i + 1]
	var plan = JSON.parse_string(FileAccess.get_file_as_string(opt["plan"]))
	var out: String = opt["out"]
	DirAccess.make_dir_recursive_absolute(out)
	var ppm := float(plan["px_per_m"])
	var shader := load(plan["shader"]) as Shader
	var textures := []
	for s in plan["sheets"]:
		var img := Image.load_from_file(s["path"])
		if img == null:
			push_error("cannot load " + s["path"])
			quit(1)
			return
		img.convert(Image.FORMAT_RGBA8)
		textures.append(ImageTexture.create_from_image(img))
	var saved := []
	var total := 0
	var status := "ok"
	for r in plan["renders"]:
		var scale := int(r["scale"])
		var w := int(r["width"])
		var h := int(r["height"])
		var vp := SubViewport.new()
		vp.size = Vector2i(w * scale, h * scale)
		vp.own_world_3d = true
		vp.transparent_bg = true
		vp.msaa_3d = Viewport.MSAA_DISABLED
		vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
		root.add_child(vp)
		var cam := Camera3D.new()
		cam.projection = Camera3D.PROJECTION_ORTHOGONAL
		cam.keep_aspect = Camera3D.KEEP_HEIGHT
		cam.size = float(h) / ppm
		cam.near = 0.1
		cam.far = 20.0
		cam.position = Vector3(0, 0, 10)
		vp.add_child(cam)
		cam.current = true
		for q in r["quads"]:
			var sheet: Dictionary = plan["sheets"][int(q["sheet"])]
			var cellpx := float(sheet["cell"])
			var mesh := QuadMesh.new()
			mesh.size = Vector2.ONE * cellpx / ppm
			var mat := ShaderMaterial.new()
			mat.shader = shader
			mat.set_shader_parameter("sheet", textures[int(q["sheet"])])
			mat.set_shader_parameter("grid", Vector2(sheet["columns"], 8))
			mat.set_shader_parameter("cell", Vector2(q["col"], q["row"]))
			mat.set_shader_parameter("cell_pixels", cellpx)
			var mi := MeshInstance3D.new()
			mi.mesh = mesh
			mi.material_override = mat
			mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
			mi.position = Vector3((q["x"] + cellpx / 2.0 - w / 2.0) / ppm, (h / 2.0 - (q["y"] + cellpx / 2.0)) / ppm, 0)
			vp.add_child(mi)
		await process_frame
		await process_frame
		await RenderingServer.frame_post_draw
		var path: String = "%s/%s.png" % [out, r["name"]]
		vp.get_texture().get_image().save_png(path)
		total += FileAccess.open(path, FileAccess.READ).get_length()
		saved.append(r["name"])
		vp.queue_free()
		await process_frame
		if total > int(plan["max_raw_bytes"]):
			status = "budget"
			break
	print(JSON.stringify({"status": status, "saved": saved.size(), "planned": plan["renders"].size(), "bytes": total}))
	quit(0 if status == "ok" and saved.size() == plan["renders"].size() else 1)
