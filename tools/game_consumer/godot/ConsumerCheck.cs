// SonoranLab FALLBACK render (DYL-131/133), NOT a game consumer check: run.sh prefers the game's own --pixel-check and
// --pixel-gallery-capture; this runs only with --fallback-render and is reported as fallback_render_non_consumer.
// Copied by tools/game_consumer/run.sh into a DISPOSABLE game snapshot only (never the real checkout) and removed after. It contains no game source: it calls the game's own consumer
// code (PixelCharacter.LoadAll, PixelGallery.Check, PixelStandee.LoadCharacter/PresentClip with the real
// PixelStandee.gdshader) and renders the new character beside two siblings at 1x and 4x (nearest).
// Args after `--`: --character ID --siblings A,B --out DIR [--max-bytes N]. Prints one line
// `SONORANLAB_CONSUMER_RESULT {json}` and writes DIR/godot_result.json; exit code 0 = pass.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Godot;
using TwosAreUp.Game;

namespace SonoranLab.ConsumerCheck;

public partial class ConsumerCheck : Node3D
{
    private const float PxPerMetre = 62f;
    private const int Gutter = 8;
    private readonly List<string> _failures = new();
    private readonly Dictionary<string, object?> _result = new();
    private long _bytes;
    private long _maxBytes = 64L * 1024 * 1024;
    private string _out = "";

    public override async void _Ready()
    {
        int code = 1;
        try
        {
            await Run();
            code = _failures.Count == 0 ? 0 : 1;
        }
        catch (Exception e)
        {
            _failures.Add($"exception: {e.GetType().Name}: {e.Message}");
        }
        _result["pass"] = _failures.Count == 0;
        _result["failures"] = _failures;
        _result["bytes_written"] = _bytes;
        string json = JsonSerializer.Serialize(_result);
        if (_out.Length > 0)
        {
            File.WriteAllText(Path.Combine(_out, "godot_result.json"), json);
        }
        GD.Print("SONORANLAB_CONSUMER_RESULT " + json);
        GetTree().Quit(_failures.Count == 0 ? code : 1);
    }

    private Dictionary<string, string> Args()
    {
        var args = new Dictionary<string, string>();
        string[] a = OS.GetCmdlineUserArgs();
        for (int i = 0; i + 1 < a.Length; i += 2) { args[a[i].TrimStart('-')] = a[i + 1]; }
        return args;
    }

    private async Task Run()
    {
        var args = Args();
        string id = args["character"];
        string[] siblingIds = args["siblings"].Split(',', StringSplitOptions.RemoveEmptyEntries);
        _out = args["out"];
        if (args.TryGetValue("max-bytes", out var mb)) { _maxBytes = long.Parse(mb); }
        Directory.CreateDirectory(_out);
        _result["character"] = id;
        _result["siblings"] = siblingIds;

        // (1) the game's manifest reader
        var all = PixelCharacter.LoadAll();
        _result["manifest_characters"] = all.Count;
        var byId = all.ToDictionary(c => c.Id);
        if (!byId.TryGetValue(id, out var subject)) { _failures.Add($"PixelCharacter.LoadAll has no {id}"); return; }
        var siblings = siblingIds.Where(byId.ContainsKey).Select(s => byId[s]).ToList();
        if (siblings.Count != siblingIds.Length) { _failures.Add("a sibling is missing from the manifest"); }
        using (var sheet = GD.Load<Texture2D>(subject.SheetPath).GetImage())
        {
            bool size = sheet.GetWidth() == subject.Columns * subject.Cell && sheet.GetHeight() == 8 * subject.Cell;
            if (!size) { _failures.Add($"{id}: sheet {sheet.GetWidth()}x{sheet.GetHeight()} != columns/cell layout"); }
        }
        int frames = subject.Clips.Values.Sum(c => c.Frames);
        if (frames + 1 != subject.Columns) { _failures.Add($"{id}: clip frames + still != columns"); }
        foreach (string need in new[] { "idle", "run" })
        {
            if (!subject.Clips.ContainsKey(need)) { _failures.Add($"{id}: no {need} clip (PixelStandee needs it)"); }
        }
        _result["entry"] = new Dictionary<string, object>
        {
            ["cell"] = subject.Cell, ["columns"] = subject.Columns, ["height_m"] = subject.HeightMetres,
            ["clips"] = subject.Clips.ToDictionary(kv => kv.Key, kv => new[] { kv.Value.Start, kv.Value.Frames }),
        };
        _result["missing_consumer_clips"] = new[] { "idle", "run", "cast", "death" }.Where(c => !subject.Clips.ContainsKey(c)).ToList();

        // (2) the game's own --pixel-check over every manifest sheet (expected to stop at the new id's clip table
        // when it has no death clip: a known missing clip, reported, not hidden)
        string pixelCheck;
        try { PixelGallery.Check(); pixelCheck = "pass"; }
        catch (Exception e) { pixelCheck = e.Message; }
        bool knownGap = pixelCheck == $"{id}: clip table" && !subject.Clips.ContainsKey("death");
        _result["game_pixel_check"] = new Dictionary<string, object>
        {
            ["message"] = pixelCheck,
            ["verdict"] = pixelCheck == "pass" ? "pass" : knownGap ? "known_missing_clip" : "fail",
        };
        if (pixelCheck != "pass" && !knownGap) { _failures.Add("PixelGallery.Check: " + pixelCheck); }

        // (3) PixelStandee with the real shader, beside the siblings, every row x (still + every clip frame)
        await Render(subject, siblings);
    }

    private async Task Render(PixelCharacter subject, List<PixelCharacter> siblings)
    {
        var camera = new OrbitCamera(); // only its Yaw/Pitch are read by PixelStandee; not added to the tree
        float yaw = camera.Yaw;
        var basis = new Basis(Vector3.Up, yaw);
        var cast = new List<PixelCharacter>();
        if (siblings.Count > 0) { cast.Add(siblings[0]); }
        cast.Add(subject);
        cast.AddRange(siblings.Skip(1));
        var standees = new List<(PixelStandee Standee, PixelCharacter Character, int Left)>();
        int cursor = Gutter;
        foreach (var c in cast)
        {
            var s = new PixelStandee { Lean = false, Name = "Standee_" + c.Id };
            AddChild(s);
            s.LoadCharacter(c);
            s.Position = basis * new Vector3((cursor + c.Cell / 2f) / PxPerMetre, 0, 0);
            standees.Add((s, c, cursor));
            cursor += c.Cell + Gutter;
        }
        int width = cursor, height = cast.Max(c => c.Cell) + 2 * Gutter;
        var views = new List<(SubViewport Vp, int Scale)>();
        foreach (int scale in new[] { 1, 4 })
        {
            var vp = new SubViewport
            {
                Size = new Vector2I(width * scale, height * scale),
                RenderTargetUpdateMode = SubViewport.UpdateMode.Always,
                Msaa3D = Viewport.Msaa.Disabled,
                ScreenSpaceAA = Viewport.ScreenSpaceAAEnum.Disabled,
                UseTaa = false,
            };
            AddChild(vp);
            var cam = new Camera3D { Projection = Camera3D.ProjectionType.Orthogonal, Size = height / PxPerMetre, Near = 0.05f, Far = 100f };
            vp.AddChild(cam);
            cam.GlobalTransform = new Transform3D(basis, basis * new Vector3(width / 2f / PxPerMetre, (height / 2f - Gutter) / PxPerMetre, 20f));
            cam.Current = true;
            views.Add((vp, scale));
        }

        // baseline without the standees: "drawn" means differs from it in that standee's slot
        foreach (var s in standees) { s.Standee.Visible = false; }
        var baseline = new List<byte[]>();
        await Frames(3);
        foreach (var v in views) { using var img = v.Vp.GetTexture().GetImage(); img.Convert(Image.Format.Rgba8); baseline.Add(img.GetData()); }
        foreach (var s in standees) { s.Standee.Visible = true; }

        var poses = new List<(string? Clip, int Frame)> { (null, 0) };
        foreach (var (name, clip) in subject.Clips) { for (int f = 0; f < clip.Frames; f++) { poses.Add((name, f)); } }
        int captures = 0, empty = 0, noBorder = 0;
        var distinct = new Dictionary<string, HashSet<string>>();
        for (int row = 0; row < 8; row++)
        {
            float facing = yaw + row * Mathf.Pi / 4;
            if (PixelStandee.Direction(facing, yaw) != row) { _failures.Add($"PixelStandee.Direction maps row {row} elsewhere"); }
            foreach (var (clipName, frame) in poses)
            {
                foreach (var (standee, c, _) in standees)
                {
                    string? clip = clipName is not null && c.Clips.ContainsKey(clipName) ? clipName : null;
                    double time = clip is null ? 0 : (frame % c.Clips[clip].Frames + 0.5) / c.Clips[clip].Fps;
                    if (c == subject && clip is not null && c.Clips[clip].Column(time) != c.Clips[clip].Start + frame)
                    {
                        _failures.Add($"{c.Id} {clip} frame {frame}: PixelClip.Column != start + frame");
                    }
                    standee.PresentClip(camera, facing, clip, time);
                }
                await Frames(2);
                string tag = $"r{row}_{clipName ?? "still"}_{frame}";
                for (int i = 0; i < views.Count; i++)
                {
                    var (vp, scale) = views[i];
                    using var img = vp.GetTexture().GetImage();
                    img.Convert(Image.Format.Rgba8);
                    string file = Path.Combine(_out, $"{tag}_{scale}x.png");
                    if (img.SavePng(file) != Error.Ok) { _failures.Add($"cannot write {file}"); continue; }
                    _bytes += new FileInfo(file).Length;
                    captures++;
                    if (_bytes > _maxBytes) { _failures.Add($"output budget {_maxBytes} bytes exceeded; stopped"); return; }
                    if (scale != 1) { continue; }
                    var subjectSlot = standees.First(s => s.Character == subject);
                    var (changed, white) = Slot(img.GetData(), baseline[i], width, height, subjectSlot.Left, subject.Cell);
                    if (changed == 0) { empty++; _failures.Add($"{tag}: {subject.Id} not drawn"); }
                    if (white == 0) { noBorder++; }
                    string key = $"r{row}_{clipName ?? "still"}";
                    if (!distinct.ContainsKey(key)) { distinct[key] = new HashSet<string>(); }
                    distinct[key].Add(Hash(img.GetData(), width, height, subjectSlot.Left, subject.Cell));
                }
            }
        }
        foreach (var (key, set) in distinct)
        {
            string clip = key.Split('_', 2)[1];
            if (clip != "still" && subject.Clips[clip].Loop && set.Count < 2) { _failures.Add($"{key}: every frame renders the same"); }
        }
        _result["render"] = new Dictionary<string, object>
        {
            ["camera_yaw"] = yaw, ["size_1x"] = new[] { width, height }, ["poses_per_row"] = poses.Count,
            ["captures"] = captures, ["empty"] = empty, ["subject_without_white_border"] = noBorder,
            ["distinct_frames"] = distinct.ToDictionary(kv => kv.Key, kv => kv.Value.Count),
            ["order"] = cast.Select(c => c.Id).ToList(),
        };
        if (noBorder > 0) { _failures.Add($"{noBorder} captures show no PixelStandee border (white) around {subject.Id}"); }
        camera.Free();
    }

    private async Task Frames(int n)
    {
        for (int i = 0; i < n; i++) { await ToSignal(RenderingServer.Singleton, RenderingServer.SignalName.FramePostDraw); }
    }

    private static (int Changed, int White) Slot(byte[] img, byte[] baseline, int width, int height, int left, int cell)
    {
        int changed = 0, white = 0;
        for (int y = 0; y < height; y++)
            for (int x = Math.Max(0, left - Gutter / 2); x < Math.Min(width, left + cell + Gutter / 2); x++)
            {
                int i = (y * width + x) * 4;
                if (img[i] != baseline[i] || img[i + 1] != baseline[i + 1] || img[i + 2] != baseline[i + 2]) { changed++; }
                if (img[i] == 255 && img[i + 1] == 255 && img[i + 2] == 255) { white++; }
            }
        return (changed, white);
    }

    private static string Hash(byte[] img, int width, int height, int left, int cell)
    {
        var bytes = new List<byte>();
        for (int y = 0; y < height; y++) { bytes.AddRange(new ArraySegment<byte>(img, (y * width + left) * 4, Math.Min(cell, width - left) * 4)); }
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(bytes.ToArray()));
    }
}
