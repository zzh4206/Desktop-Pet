// Toon 阶梯光 —— CustomMaterial 片元钩子（S2.3 首刀）。
// Qt 6.x API：零参数钩子 + 扁平特殊变量。阶梯两段 + rim 边缘光。
// 透明度走 BASE_COLOR 的 alpha 通道（此 API 无独立 ALPHA 变量，写它=编译失败隐身）。
// ⚠️ BASE_COLOR 只在 MAIN 钩子可用——光照钩子（DIRECTIONAL_LIGHT/AMBIENT_LIGHT）
// 里引用它=SPIR-V 编译失败=材质无效=模型隐身（蒙皮排列下实测复现）。基色在
// MAIN 里采好存全局 g_base，光照钩子只读 g_base。
vec3 g_base = vec3(0.5, 0.5, 0.55);

void MAIN()
{
    vec4 c = uHasTex > 0.5 ? texture(uBaseTex, UV0) : uBase;
    BASE_COLOR = c;
    g_base = c.rgb;
}

void DIRECTIONAL_LIGHT()
{
    float ndl = clamp(dot(normalize(VAR_WORLD_NORMAL), normalize(TO_LIGHT_DIR)), 0.0, 1.0);
    float band = ndl > uStep ? 1.0 : 0.55;
    DIFFUSE += LIGHT_COLOR * g_base * band * uLightGain;
}

void AMBIENT_LIGHT()
{
    vec3 n = normalize(VAR_WORLD_NORMAL);
    vec3 v = normalize(CAMERA_POSITION - VAR_WORLD_POSITION);
    float rim = pow(1.0 - clamp(dot(n, v), 0.0, 1.0), 3.0);
    DIFFUSE = min(DIFFUSE + uAmbient * g_base + uRim * rim, vec3(1.2));
}
