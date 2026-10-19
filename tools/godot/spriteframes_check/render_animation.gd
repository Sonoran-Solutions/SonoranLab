# Visual proof: draw one animation of an exported SpriteFrames at 1x and 4x (nearest filter, project default) and save
# the viewport after each frame as PNG. Needs a real renderer: run through render.sh, on an already imported project.
#
#   render.sh <project copy> -- --tres res://toy/toy_frames.tres --anim run_east --out DIR [--frames 4]
# Writes DIR/<anim>_f<i>.png (a SubViewport sized to the frame w x h: (24 + 5w) x (16 + 4h), 1x sprite at (8, 8), 4x
# sprite at (16 + w, 8), grey background) and prints a JSON line with the sprite frame index seen in each capture, then
# a short real-time playback sample. The SubViewport takes the project's default texture filter from the root.
extends SceneTree

func _initialize() -> void:
	var a := OS.get_cmdline_user_args()
	var opt := {"frames": "0"}
	for i in range(0, a.size() - 1, 2):
		opt[a[i].trim_prefix("--")] = a[i + 1]
	var frames := load(opt["tres"]) as SpriteFrames
	var anim: String = opt["anim"]
	var out: String = opt["out"]
	DirAccess.make_dir_recursive_absolute(out)
	var cell: Vector2 = frames.get_frame_texture(anim, 0).get_size()
	var vp := SubViewport.new()
	vp.size = Vector2i(int(24 + 5 * cell.x), int(16 + 4 * cell.y))
	vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	vp.canvas_item_default_texture_filter = root.canvas_item_default_texture_filter
	root.add_child(vp)
	var bg := ColorRect.new()
	bg.color = Color(0.5, 0.5, 0.5)
	bg.size = Vector2(vp.size)
	vp.add_child(bg)
	var s1 := AnimatedSprite2D.new()
	var s4 := AnimatedSprite2D.new()
	for s in [s1, s4]:
		s.sprite_frames = frames
		s.animation = anim
		s.centered = false
		vp.add_child(s)
	s1.position = Vector2(8, 8)
	s4.position = Vector2(16 + cell.x, 8)
	s4.scale = Vector2(4, 4)
	var n := frames.get_frame_count(anim)
	var count := int(opt["frames"]) if int(opt["frames"]) > 0 else n
	var seen := []
	for i in range(count):
		s1.frame = i % n
		s4.frame = i % n
		await process_frame
		await process_frame
		vp.get_texture().get_image().save_png("%s/%s_f%d.png" % [out, anim, i])
		seen.append(s4.frame)
	s1.play(anim)
	s4.play(anim)
	var live := []
	for i in range(12):
		await process_frame
		await create_timer(0.05).timeout
		live.append(s4.frame)
	print(JSON.stringify({"anim": anim, "captured_frames": seen, "live_frames_every_50ms": live, "fps": frames.get_animation_speed(anim)}))
	quit()
