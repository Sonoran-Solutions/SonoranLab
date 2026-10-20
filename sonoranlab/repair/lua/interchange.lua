-- SonoranLab-owned batch interchange. Only the host generates plan.json and these two fixed parameters.
-- The sandbox mounts this script read-only; no extension or agent-supplied Lua is executed.
local function read_json(path)
  local f = assert(io.open(path, "r"))
  local result = json.decode(f:read("*a"))
  f:close()
  return result
end

local function write_json(path, value)
  local f = assert(io.open(path, "w"))
  f:write(json.encode(value), "\n")
  f:close()
end

local function palette_hex(palette)
  local result = {}
  for i = 0, #palette - 1 do
    local c = palette:getColor(i)
    result[#result + 1] = string.format("#%02x%02x%02x%02x", c.red, c.green, c.blue, c.alpha)
  end
  return result
end

local function layer_names(layers, result)
  for _, layer in ipairs(layers) do
    result[#result + 1] = layer.name
    if layer.isGroup then layer_names(layer.layers, result) end
  end
end

if app.params.action == "build" then
  local plan = read_json("/work/plan.json")
  local s = Sprite(plan.width, plan.height, ColorMode.RGB)
  local layer = s.layers[1]
  layer.name = "art"
  local palette = Palette(#plan.palette + 1)
  palette:setColor(0, Color{r=0, g=0, b=0, a=0})
  for i, hex in ipairs(plan.palette) do
    palette:setColor(i, Color{
      r=tonumber(hex:sub(2, 3), 16), g=tonumber(hex:sub(4, 5), 16),
      b=tonumber(hex:sub(6, 7), 16), a=255})
  end
  s:setPalette(palette)
  for n, entry in ipairs(plan.frames) do
    assert(entry.png:match("^/work/input/[%w_%-]+%.png$"), "invalid generated input path")
    local source = assert(app.open(entry.png))
    assert(source.width == plan.width and source.height == plan.height, "canvas differs")
    -- Rendering to RGB also decodes indexed PNG palettes; no quantization/resizing/profile conversion.
    local image = Image(plan.width, plan.height, ColorMode.RGB)
    image:drawSprite(source, 1)
    source:close()
    local frame = n == 1 and s.frames[1] or s:newEmptyFrame(n)
    frame.duration = entry.duration_ms / 1000
    s:newCel(layer, frame, image, Point(0, 0))
  end
  local names = {}
  for name in pairs(plan.tags) do names[#names + 1] = name end
  table.sort(names)
  for _, name in ipairs(names) do
    local range = plan.tags[name]
    local tag = s:newTag(range[1], range[2])
    tag.name = name
  end
  s:saveAs("/work/doc.aseprite")
  s:close()
elseif app.params.action == "export" then
  local output = app.params.output or "export"
  assert(output == "export" or output:match("^export%-%d%d%d%d%d*$"), "invalid export directory")
  local root = "/work/" .. output
  local s = assert(app.open("/work/doc.aseprite"))
  local meta = {
    width=s.width, height=s.height,
    color_mode=({[ColorMode.RGB]="rgb", [ColorMode.GRAY]="grayscale", [ColorMode.INDEXED]="indexed"})[s.colorMode],
    palette=palette_hex(s.palettes[1]), layers={}, frame_count=#s.frames, durations_ms={}, tags={}}
  layer_names(s.layers, meta.layers)
  for _, tag in ipairs(s.tags) do
    meta.tags[tag.name] = {tag.fromFrame.frameNumber, tag.toFrame.frameNumber}
  end
  for n, frame in ipairs(s.frames) do
    meta.durations_ms[n] = math.floor(frame.duration * 1000 + 0.5)
    local image = Image(s.width, s.height, ColorMode.RGB)
    image:drawSprite(s, n)
    image:saveAs(root .. "/" .. n .. ".png")
  end
  write_json(root .. "/meta.json", meta)
  s:close()
else
  error("expected fixed build/export action")
end
