# Checks an exported SpriteFrames .tres against an expected.json, in the real engine (headless, no renderer).
#
#   godot --headless --path <project copy> --fixed-fps 60 --script res://check_spriteframes.gd -- \
#     res://<char>/<char>_frames.tres res://expected.json
#
# expected.json: {"sheet": "res://<char>/<char>.png", "sheet_size": [w, h],
#   "animations": {name: {"frames": [[x, y, w, h, "sha256 of the region's RGBA8 bytes"], ...], "speed": fps, "loop": bool}}}
# Per animation: it exists, has the right frame count / speed / loop, every frame is an AtlasTexture over the sheet with
# the right region whose pixels (read from the PNG file) hash as expected; then an AnimatedSprite2D plays it for real
# main-loop iterations (--fixed-fps 60 gives a fixed 1/60 s step) and the frame sequence, the advance rate, looping /
# holding and animation_finished are checked. Prints one JSON result; exit code 0 = pass, 1 = mismatch, 2 = bad input.
extends SceneTree

const STEP := 1.0 / 60.0
var frames: SpriteFrames
var expected: Dictionary
var names: Array = []
var failures: Array = []
var report: Dictionary = {}
var sprite: AnimatedSprite2D
var idx := -1
var iter := 0
var limit := 0
var seq: Array = []
var finished := 0
var warmup := 0

func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 2:
		_bail(2, "usage: -- <res path of .tres> <res path of expected.json>")
		return
	frames = load(args[0]) as SpriteFrames
	if frames == null:
		_bail(2, "cannot load %s as SpriteFrames" % args[0])
		return
	var f := FileAccess.open(args[1], FileAccess.READ)
	if f == null:
		_bail(2, "cannot read " + args[1])
		return
	expected = JSON.parse_string(f.get_as_text())
	_check_static()
	names = expected["animations"].keys()
	sprite = AnimatedSprite2D.new()
	sprite.sprite_frames = frames
	root.add_child(sprite)
	sprite.animation_finished.connect(func(): finished += 1)

func _bail(code: int, msg: String) -> void:
	print(JSON.stringify({"pass": false, "error": msg}))
	quit(code)

func _fail(msg: String) -> void:
	failures.append(msg)

func _check_static() -> void:
	var have := Array(frames.get_animation_names())
	have.sort()
	var want: Array = expected["animations"].keys()
	want.sort()
	if have != want:
		_fail("animation names differ: have %s want %s" % [have, want])
	var img := Image.load_from_file(ProjectSettings.globalize_path(expected["sheet"]))
	if img == null:
		_fail("cannot read the sheet PNG " + expected["sheet"])
		return
	img.convert(Image.FORMAT_RGBA8)
	if [img.get_width(), img.get_height()] != [int(expected["sheet_size"][0]), int(expected["sheet_size"][1])]:
		_fail("sheet size %dx%d != %s" % [img.get_width(), img.get_height(), expected["sheet_size"]])
	var sheet_path := ProjectSettings.localize_path(expected["sheet"])
	for name in want:
		if not frames.has_animation(name):
			continue
		var e: Dictionary = expected["animations"][name]
		if frames.get_frame_count(name) != e["frames"].size():
			_fail("%s: %d frames, want %d" % [name, frames.get_frame_count(name), e["frames"].size()])
			continue
		if not is_equal_approx(frames.get_animation_speed(name), e["speed"]):
			_fail("%s: speed %s, want %s" % [name, frames.get_animation_speed(name), e["speed"]])
		if frames.get_animation_loop(name) != e["loop"]:
			_fail("%s: loop %s, want %s" % [name, frames.get_animation_loop(name), e["loop"]])
		for i in e["frames"].size():
			var t := frames.get_frame_texture(name, i) as AtlasTexture
			var w: Array = e["frames"][i]
			if t == null or t.atlas == null or t.atlas.resource_path != sheet_path:
				_fail("%s[%d]: not an AtlasTexture over %s" % [name, i, sheet_path])
				continue
			var r := t.region
			if r != Rect2(w[0], w[1], w[2], w[3]):
				_fail("%s[%d]: region %s, want %s" % [name, i, r, Rect2(w[0], w[1], w[2], w[3])])
				continue
			var ctx := HashingContext.new()
			ctx.start(HashingContext.HASH_SHA256)
			ctx.update(img.get_region(Rect2i(r)).get_data())
			if ctx.finish().hex_encode() != w[4]:
				_fail("%s[%d]: pixels at %s differ from the expected cell" % [name, i, r])
			if frames.get_frame_duration(name, i) != 1.0:
				_fail("%s[%d]: relative duration %s != 1.0" % [name, i, frames.get_frame_duration(name, i)])
	report["sheet"] = expected["sheet"]
	report["animations_checked"] = want.size()

func _start_next() -> void:
	idx += 1
	if idx >= names.size():
		return
	var name: String = names[idx]
	var e: Dictionary = expected["animations"][name]
	var n: int = e["frames"].size()
	var secs: float = 2.0 if e["loop"] else n / e["speed"] + 0.5
	limit = int(round(secs / STEP))
	iter = 0
	seq = []
	finished = 0
	sprite.play(name)
	warmup = 1

func _check_playback() -> void:
	var name: String = names[idx]
	var e: Dictionary = expected["animations"][name]
	var n: int = e["frames"].size()
	var changes := 0
	var order_ok := true
	for i in range(1, seq.size()):
		if seq[i] != seq[i - 1]:
			changes += 1
			var step: int = (seq[i] - seq[i - 1] + n) % n
			if step != 1 or (not e["loop"] and seq[i] < seq[i - 1]):
				order_ok = false
	var secs: float = limit * STEP
	var want_changes := int(floor(e["speed"] * secs))
	if not e["loop"]:
		want_changes = n - 1
	var r := {"fps": e["speed"], "loop": e["loop"], "iterations": limit, "changes": changes, "last_frame": seq[-1], "finished": finished}
	if not order_ok:
		_fail("%s: frames did not advance 0,1,2,.. in order: %s" % [name, seq])
	if abs(changes - want_changes) > 1 or (e["loop"] and n == 1 and changes != 0):
		_fail("%s: %d frame changes in %.2f s, want %d (+-1) at %s fps" % [name, changes, secs, want_changes, e["speed"]])
	if e["loop"]:
		if finished != 0 or not sprite.is_playing() and n > 1:
			_fail("%s: a looping animation must keep playing and never finish (finished=%d)" % [name, finished])
	else:
		if seq[-1] != n - 1 or seq.count(n - 1) < 2:
			_fail("%s: non-looping animation must reach and hold frame %d, ended on %d" % [name, n - 1, seq[-1]])
		if finished != 1:
			_fail("%s: animation_finished emitted %d times, want 1" % [name, finished])
		if sprite.is_playing():
			_fail("%s: still playing after the last frame" % name)
	report[name] = r

func _process(_delta: float) -> bool:
	if idx < 0:
		_start_next()
		return false
	if idx >= names.size():
		var ok := failures.is_empty()
		report["pass"] = ok
		report["failures"] = failures
		print(JSON.stringify(report))
		quit(0 if ok else 1)
		return true
	if warmup > 0:  # let the first node process tick settle (it may or may not advance this iteration)
		warmup -= 1
		return false
	seq.append(sprite.frame)
	iter += 1
	if iter >= limit:
		_check_playback()
		_start_next()
	return false
