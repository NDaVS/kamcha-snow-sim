"""Assemble overlapping OSM Map API tiles offline, keeping the newest object versions."""
import argparse
import gzip
from pathlib import Path
from xml.etree import ElementTree as ET


def merge(paths, output):
    root = ET.Element("osm", version="0.6", generator="snowops-mosaic-1",
                      attribution="https://www.openstreetmap.org/copyright",
                      license="https://opendatacommons.org/licenses/odbl/1-0/")
    objects = {}
    for path in paths:
        data = path.read_bytes()
        tile = ET.fromstring(gzip.decompress(data) if path.suffix == ".gz" else data)
        for bounds in tile.findall("bounds"):
            root.append(bounds)
        for obj in tile:
            if obj.tag not in {"node", "way", "relation"}:
                continue
            key = obj.tag, int(obj.attrib["id"])
            previous = objects.get(key)
            if previous is None or int(obj.get("version", 0)) > int(previous.get("version", 0)):
                objects[key] = obj
    for key in sorted(objects):
        root.append(objects[key])
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output.write_bytes(gzip.compress(data, mtime=0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("tiles", nargs="+", type=Path)
    args = parser.parse_args()
    merge(args.tiles, args.output)
