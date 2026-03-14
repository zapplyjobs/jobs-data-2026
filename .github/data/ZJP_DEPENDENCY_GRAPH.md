# ZJP Dependency Graph

Generated from submodule `922ed07`. Entry points: `index.js`, `lib/aggregator/processors/tag-engine.js`.

flowchart LR

0["index.js"]
subgraph 1["config"]
2["index.js"]
3["api-limits.js"]
4["categories.js"]
5["locations.js"]
end
subgraph 6["lib"]
7["utils.js"]
subgraph B["aggregator"]
subgraph C["processors"]
D["tag-engine.js"]
end
end
end
8["crypto"]
9["fs"]
A["path"]
0-->2
0-->7
2-->3
2-->4
2-->5
7-->8
7-->9
7-->A
