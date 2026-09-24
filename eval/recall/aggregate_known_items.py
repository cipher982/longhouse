import json, sys, collections
for path in sys.argv[1:]:
    d = json.load(open(path))
    for phase in ("cold", "warm"):
        c = d[phase]; cat = collections.Counter(); hit = collections.Counter()
        for key, v in c["by_provider_category"].items():
            k = key.split("/", 1)[1]; cat[k] += v["total"]; hit[k] += v["hits"]
        tot, hits = sum(cat.values()), sum(hit.values())
        lat = c.get("latency_seconds", {})
        print(f"{path.split('/')[-2]:>11} {path.split('_')[-1][:-5]:>7} {phase}: {hits}/{tot} = {hits/max(tot,1):.3f} errors={c['errors']} p50={lat.get('p50',0):.3f} p95={lat.get('p95',0):.3f} p99={lat.get('p99',0):.3f}")
        if phase == "cold":
            print("      ", {k: f"{hit[k]}/{cat[k]}" for k in cat})
