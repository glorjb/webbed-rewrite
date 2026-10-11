// Non-interactive UTMT export for Desktop Playground.
// Writes ORIGINAL GAME ASSETS to the directory in env DP_OUT (must be outside the repo).
// Usage: UndertaleModCli load <data.win> -s ExportWebbed.csx
using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Collections.Generic;
using UndertaleModLib.Util;

EnsureDataLoaded();
string outDir = Environment.GetEnvironmentVariable("DP_OUT");
if (string.IsNullOrEmpty(outDir)) throw new Exception("set DP_OUT");
Directory.CreateDirectory(outDir);
var jsonOpts = new JsonSerializerOptions { WriteIndented = true };
string Sub(string name) { var p = Path.Combine(outDir, name); Directory.CreateDirectory(p); return p; }

// ---------- general info ----------
var gen = Data.GeneralInfo;
File.WriteAllText(Path.Combine(outDir, "general.json"), JsonSerializer.Serialize(new {
    name = gen.Name?.Content, config = gen.Config?.Content, bytecode = gen.BytecodeVersion,
    gmVersion = $"{gen.Major}.{gen.Minor}.{gen.Release}.{gen.Build}",
    detectedVersion = Data.IsVersionAtLeast(2023, 1) ? ">=2023.1" : Data.IsVersionAtLeast(2022, 1) ? ">=2022.1" : Data.IsVersionAtLeast(2, 3, 2) ? ">=2.3.2" : Data.IsVersionAtLeast(2, 3) ? ">=2.3" : "<2.3",
    isYYC = Data.IsYYC(), gameSpeed = gen.GMS2FPS, windowW = gen.DefaultWindowWidth, windowH = gen.DefaultWindowHeight,
    info = gen.Info.ToString(), timestamp = gen.Timestamp,
}, jsonOpts));

// ---------- texture sheets ----------
var texDir = Sub("texture_pages");
for (int i = 0; i < Data.EmbeddedTextures.Count; i++)
{
    try
    {
        using FileStream fs = new(Path.Combine(texDir, $"{i}.png"), FileMode.Create);
        Data.EmbeddedTextures[i].TextureData.Image.SavePng(fs);
    }
    catch (Exception e) { File.AppendAllText(Path.Combine(outDir, "errors.txt"), $"tex {i}: {e.Message}\n"); }
}

// ---------- sprites: frames + metadata ----------
var sprDir = Sub("sprites");
var sprMeta = new List<object>();
using (var tw = new TextureWorker())
{
    foreach (var s in Data.Sprites)
    {
        var frames = new List<object>();
        for (int i = 0; i < s.Textures.Count; i++)
        {
            var ti = s.Textures[i]?.Texture;
            if (ti == null) continue;
            string fn = $"{s.Name.Content}_{i}.png";
            try { tw.ExportAsPNG(ti, Path.Combine(sprDir, fn), null, true); } catch (Exception e) { File.AppendAllText(Path.Combine(outDir, "errors.txt"), $"spr {s.Name.Content}: {e.Message}\n"); }
            frames.Add(new { file = fn, page = ti.TexturePage?.Name?.Content, srcX = ti.SourceX, srcY = ti.SourceY, srcW = ti.SourceWidth, srcH = ti.SourceHeight,
                targetX = ti.TargetX, targetY = ti.TargetY, targetW = ti.TargetWidth, targetH = ti.TargetHeight, boundW = ti.BoundingWidth, boundH = ti.BoundingHeight });
        }
        sprMeta.Add(new {
            name = s.Name.Content, width = s.Width, height = s.Height, originX = s.OriginX, originY = s.OriginY,
            bboxLeft = s.MarginLeft, bboxRight = s.MarginRight, bboxTop = s.MarginTop, bboxBottom = s.MarginBottom,
            bboxMode = s.BBoxMode, sepMasks = s.SepMasks.ToString(), transparent = s.Transparent, smooth = s.Smooth, preload = s.Preload,
            type = s.SSpriteType.ToString(), playbackSpeed = s.GMS2PlaybackSpeed, playbackSpeedType = s.GMS2PlaybackSpeedType.ToString(),
            isSpecial = s.IsSpecialType, specialVersion = s.SVersion, frameCount = s.Textures.Count, frames,
        });
    }
}
File.WriteAllText(Path.Combine(outDir, "sprites.json"), JsonSerializer.Serialize(sprMeta, jsonOpts));

// ---------- shaders ----------
var shDir = Sub("shaders");
foreach (var sh in Data.Shaders)
{
    var d = Path.Combine(shDir, sh.Name.Content); Directory.CreateDirectory(d);
    File.WriteAllText(Path.Combine(d, "type.txt"), sh.Type.ToString());
    File.WriteAllText(Path.Combine(d, "GLSL_ES.vsh"), sh.GLSL_ES_Vertex?.Content ?? "");
    File.WriteAllText(Path.Combine(d, "GLSL_ES.fsh"), sh.GLSL_ES_Fragment?.Content ?? "");
    File.WriteAllText(Path.Combine(d, "GLSL.vsh"), sh.GLSL_Vertex?.Content ?? "");
    File.WriteAllText(Path.Combine(d, "GLSL.fsh"), sh.GLSL_Fragment?.Content ?? "");
    File.WriteAllText(Path.Combine(d, "HLSL9.vsh"), sh.HLSL9_Vertex?.Content ?? "");
    File.WriteAllText(Path.Combine(d, "HLSL9.fsh"), sh.HLSL9_Fragment?.Content ?? "");
    File.WriteAllText(Path.Combine(d, "attributes.txt"), string.Join("\n", sh.VertexShaderAttributes.Select(a => a.Name.Content)));
}

// ---------- audio ----------
var audDir = Sub("audio");
var groupCache = new Dictionary<int, IList<UndertaleEmbeddedAudio>>();
IList<UndertaleEmbeddedAudio> GroupAudio(int gid)
{
    if (gid == 0) return Data.EmbeddedAudio;
    if (groupCache.TryGetValue(gid, out var c)) return c;
    var path = Path.Combine(Path.GetDirectoryName(FilePath), $"audiogroup{gid}.dat");
    using var fs = new FileStream(path, FileMode.Open, FileAccess.Read);
    var gd = UndertaleIO.Read(fs);
    groupCache[gid] = gd.EmbeddedAudio; return gd.EmbeddedAudio;
}
var sndMeta = new List<object>();
foreach (var s in Data.Sounds)
{
    string file = null; string err = null;
    bool compressed = s.Flags.HasFlag(UndertaleSound.AudioEntryFlags.IsCompressed);
    bool embedded = s.Flags.HasFlag(UndertaleSound.AudioEntryFlags.IsEmbedded);
    try
    {
        if (s.AudioID >= 0 && (embedded || compressed))
        {
            var bytes = GroupAudio(s.GroupID)[s.AudioID].Data;
            string ext = (bytes.Length > 4 && bytes[0] == (byte)'O' && bytes[1] == (byte)'g') ? ".ogg" : ".wav";
            file = s.Name.Content + ext;
            File.WriteAllBytes(Path.Combine(audDir, file), bytes);
        }
        else err = "external: " + s.File?.Content;
    }
    catch (Exception e) { err = e.Message; }
    sndMeta.Add(new { name = s.Name.Content, file, error = err, group = s.AudioGroup?.Name?.Content, groupId = s.GroupID,
        volume = s.Volume, pitch = s.Pitch, flags = s.Flags.ToString(), type = s.Type?.Content, sourceFile = s.File?.Content, effects = s.Effects, preload = s.Preload });
}
File.WriteAllText(Path.Combine(outDir, "sounds.json"), JsonSerializer.Serialize(sndMeta, jsonOpts));

// ---------- objects (events present, physics, parents) ----------
var objMeta = Data.GameObjects.Select(o => new {
    name = o.Name.Content, sprite = o.Sprite?.Name?.Content, mask = o.TextureMaskId?.Name?.Content, parent = o.ParentId?.Name?.Content,
    visible = o.Visible, solid = o.Solid, depth = o.Depth, persistent = o.Persistent,
    usesPhysics = o.UsesPhysics, isSensor = o.IsSensor, shape = o.CollisionShape.ToString(), density = o.Density, restitution = o.Restitution,
    group = o.Group, linearDamping = o.LinearDamping, angularDamping = o.AngularDamping, friction = o.Friction, awake = o.Awake, kinematic = o.Kinematic,
    physicsVertices = o.PhysicsVertices.Select(v => new[] { v.X, v.Y }).ToArray(),
    events = o.Events.Select((list, type) => new { type = ((EventType)type).ToString(), subtypes = list.Select(e => e.EventSubtype).ToArray() }).Where(e => e.subtypes.Length > 0).ToArray(),
});
File.WriteAllText(Path.Combine(outDir, "objects.json"), JsonSerializer.Serialize(objMeta, jsonOpts));

// ---------- rooms: physics world + instance placements (for test scenes) ----------
var roomMeta = Data.Rooms.Select(r => new {
    name = r.Name.Content, width = r.Width, height = r.Height, speed = r.Speed, physicsWorld = r.World,
    gravityX = r.GravityX, gravityY = r.GravityY, metersPerPixel = r.MetersPerPixel,
    instances = r.GameObjects.Select(g => new { obj = g.ObjectDefinition?.Name?.Content, x = g.X, y = g.Y, sx = g.ScaleX, sy = g.ScaleY, rot = g.Rotation, id = g.InstanceID }).ToArray(),
});
File.WriteAllText(Path.Combine(outDir, "rooms.json"), JsonSerializer.Serialize(roomMeta, jsonOpts));

// ---------- timelines + paths + fonts list ----------
File.WriteAllText(Path.Combine(outDir, "timelines.json"), JsonSerializer.Serialize(Data.Timelines.Select(t => new { name = t.Name.Content, moments = t.Moments.Select(m => m.Step).ToArray() }), jsonOpts));
File.WriteAllText(Path.Combine(outDir, "strings.txt"), string.Join("\n", Data.Strings.Select(s => s.Content)));
Console.WriteLine("EXPORT DONE -> " + outDir);
