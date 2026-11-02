// Used when no private sticker-border shader is given: same uniforms and UV math as the game's standee shader, but no
// border is drawn. The result of a run that uses this file says `border: not_rendered`.
shader_type spatial;
render_mode unshaded, cull_disabled;
uniform sampler2D sheet : source_color, filter_nearest, repeat_disable;
uniform vec2 grid = vec2(25.0, 8.0);
uniform vec2 cell = vec2(0.0);
uniform float cell_pixels = 96.0;
void fragment() {
    vec4 ink = texture(sheet, (cell + UV) / grid);
    if (ink.a < 0.5) { discard; }
    ALBEDO = ink.rgb;
}
