import json

for stage in ("young", "adult", "final"):
    p = f"assets/rig/{stage}/manifest.json"
    mf = json.load(open(p, encoding="utf-8"))
    n = 0
    for k, v in mf["figures"].items():
        if "\\" in v:
            mf["figures"][k] = v.replace("\\", "/")
            n += 1
    for part in mf["parts"]:
        if "\\" in part["file"]:
            part["file"] = part["file"].replace("\\", "/")
            n += 1
    with open(p, "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False, indent=2)
    print(stage, "normalized", n, "paths")
