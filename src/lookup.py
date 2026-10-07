# Python script that looks up the menu name given 2 numbers.

import json


def build_index(menus):
    index = {}

    def walk(node):
        for child in node.get("submenues", []):
            index[(child.get("button_nr"), node.get("menue_nr"))] = {
                "parent": node,
                "child": child,
            }
            walk(child)

    for menu in menus:
        walk(menu)

    return index


with open("hicad_menus.json", "r", encoding="utf-8") as f:
    data = json.load(f)

index = build_index(data["hicad_menues"])

# The OPTIONS command we are looking for like "OPTION 8 16"
result = index.get((8, 16))
if result:
    print(result["parent"]["display_name"])  # Neuzeichnen
    print(result["child"]["display_name"])  # alle Teile
