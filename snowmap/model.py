import json
import math
from collections import defaultdict
from functools import lru_cache

import numpy as np
from shapely import intersects_xy
from shapely.geometry import box, mapping

from .foam import ANGLES, HALF_LENGTH, HALF_WIDTH, HEIGHTS, KERNELS, SIZE, STEP
from .sources import projection


@lru_cache(maxsize=24)
def read_kernel(path, modified_ns):
    with path.open() as stream:
        data = json.load(stream)
    samples = np.array(data["speed_ratio"], dtype=float)
    expected_size = 2*SIZE//STEP
    if data["version"] != 1 or samples.shape != (expected_size, expected_size):
        raise ValueError(f"Unsupported kernel: {path}")
    return samples


def kernels():
    fields = {}
    for height in HEIGHTS:
        for angle in ANGLES:
            path = KERNELS / f"h{height}-a{angle}.json"
            try:
                modified = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            fields[height, angle] = read_kernel(path, modified)
    return fields


def weights(value, bins):
    value = float(np.clip(value, bins[0], bins[-1]))
    for low, high in zip(bins, bins[1:]):
        if value <= high:
            fraction = (value-low)/(high-low)
            return [(point, weight) for point, weight in ((low, 1-fraction), (high, fraction)) if weight > 1e-10]


def sample(field, x, y):
    # Bilinear interpolation, ignoring the solid cells in the reference building.
    size = field.shape[0]
    i, j = (x+SIZE)/STEP-0.5, (y+SIZE)/STEP-0.5
    valid = (i >= 0) & (i <= size-1) & (j >= 0) & (j <= size-1)
    i, j = np.clip(i, 0, size-1.001), np.clip(j, 0, size-1.001)
    ix, iy = i.astype(int), j.astype(int)
    tx, ty = i-ix, j-iy
    result, total = np.zeros_like(i), np.zeros_like(i)
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        number = field[iy+dy, ix+dx]
        usable = valid & np.isfinite(number)
        weight = (tx if dx else 1-tx)*(ty if dy else 1-ty)*usable
        result += np.where(usable, number, 0)*weight
        total += weight
    return np.divide(result, total, out=np.ones_like(result), where=total > 0)


def rectangle(building):
    points = np.asarray(building.footprint.minimum_rotated_rectangle.exterior.coords)[:4]
    edges = np.roll(points, -1, axis=0)-points
    lengths = np.linalg.norm(edges, axis=1)
    index = np.argmax(lengths)
    axis = edges[index]/lengths[index]
    return points.mean(axis=0), axis, lengths[index], lengths[(index+1)%4], points


def prepare(bounds, buildings, resolution):
    _, _, width, height = projection(bounds)
    cell = max(width, height)/resolution
    cols, rows = math.ceil(width/cell), math.ceil(height/cell)
    xs = (np.arange(cols)+0.5)*width/cols
    ys = height-(np.arange(rows)+0.5)*height/rows
    mask = np.zeros((rows, cols), dtype=bool)
    visible = []
    prepared = []
    tile = box(0, 0, width, height)
    for building in buildings:
        shape = building.footprint
        left, bottom, right, top = shape.bounds
        reach = min(350, 9*building.height)
        i = slice(xs.searchsorted(left-reach), xs.searchsorted(right+reach, side="right"))
        j = slice((-ys).searchsorted(-top-reach), (-ys).searchsorted(-bottom+reach, side="right"))
        if i.start == i.stop or j.start == j.stop:
            continue
        x, y = np.meshgrid(xs[i], ys[j])
        mask[j, i] |= intersects_xy(shape, x, y)
        prepared.append((building, rectangle(building), j, i, x, y))
        if shape.intersects(tile):
            visible.append({"geometry": mapping(shape), "height_m": building.height,
                            "height_source": building.height_source})
    return prepared, mask, visible, width, height


def estimate(bounds, buildings, weather, resolution, use_foam):
    prepared, mask, visible, width, height = prepare(bounds, buildings, resolution)
    library = kernels() if use_foam else {}
    blended = {}
    applied = 0
    active = 0
    snow = np.zeros(mask.shape)
    events = defaultdict(float)
    for hour in weather:
        events[hour["wind_speed_ms"], hour["wind_from_deg"]] += hour["snowfall_cm"]
    for (speed, direction), snowfall in events.items():
        strength = float(np.clip((speed-2)/10, 0, 1))
        if snowfall <= 0 or strength == 0:
            snow += snowfall
            continue
        angle = math.radians(direction)
        flow = np.array((-math.sin(angle), -math.cos(angle)))
        cross = np.array((-flow[1], flow[0]))
        deposit, scour = np.zeros_like(snow), np.zeros_like(snow)
        for building, (center, axis, length, depth, points), j, i, x, y in prepared:
            active += 1
            h = min(building.height, 45)
            dx, dy = x-center[0], y-center[1]
            along = dx*flow[0]+dy*flow[1]
            lateral = dx*cross[0]+dy*cross[1]
            half_along = np.max(abs((points-center) @ flow))
            half_cross = np.max(abs((points-center) @ cross))
            down = along-half_along
            up = -along-half_along
            side = np.maximum(0, abs(lateral)-half_cross)
            wake = np.where(down >= 0, np.exp(-((down-1.6*h)/(2.8*h))**2)*0.95, 0)
            wake *= np.exp(-(side/(0.35*h+0.15*np.maximum(down, 0)))**2)
            front = np.where(up >= 0, np.exp(-((up-0.5*h)/(0.55*h))**2)*0.32, 0)
            front *= np.exp(-(side/(0.45*h))**2)
            flanks = np.exp(-((side-0.35*h)/(0.45*h))**2
                            -(np.maximum(abs(along)-half_along, 0)/(1.2*h))**2)
            flanks *= abs(lateral) >= half_cross
            enhancement = wake+front
            erosion = 0.32*flanks
            if library:
                normal = np.array((-axis[1], axis[0]))
                parallel, perpendicular = flow @ axis, flow @ normal
                incident = math.degrees(math.atan2(abs(perpendicular), abs(parallel)))
                choices = tuple((height, direction, a*b) for height, a in weights(h, HEIGHTS)
                                for direction, b in weights(incident, ANGLES))
                if all((height, direction) in library for height, direction, _ in choices):
                    applied += 1
                    local_x = (dx*axis[0]+dy*axis[1])*math.copysign(2*HALF_LENGTH/length, parallel)
                    local_y = (dx*normal[0]+dy*normal[1])*math.copysign(2*HALF_WIDTH/depth, perpendicular)
                    # All templates share the same solid mask and grid, so blend before sampling.
                    if choices not in blended:
                        blended[choices] = sum(weight*library[height, direction]
                                               for height, direction, weight in choices)
                    ratio = sample(blended[choices], local_x, local_y)
                    proximity = np.exp(-((np.maximum(abs(along)-half_along, 0)/(3*h))**2
                                         +(side/(1.5*h))**2))
                    enhancement += 0.55*proximity*np.clip(0.85-ratio, 0, 0.85)
                    erosion += 0.6*proximity*np.clip(ratio-1, 0, 1)
            deposit[j, i] += enhancement
            scour[j, i] += erosion
        # Saturation limits overlapping wakes. Overlapping flank effects can
        # reduce accumulation in narrow passages between neighboring buildings.
        factor = 1+strength*(1.8*np.tanh(deposit/1.8)-0.85*np.tanh(scour/0.85))
        snow += snowfall*factor
    rendered = snow.round(3).astype(object)
    rendered[mask] = None
    return {"width_m": width, "height_m": height, "building_count": len(visible),
            "buildings_in_model": len(prepared), "footprints": visible,
            "rows": mask.shape[0], "cols": mask.shape[1], "snow_depth_cm": rendered.tolist(),
            "model": ("hybrid" if applied == active else "hybrid_partial") if applied else "analytic",
            "cfd_coverage": applied/active if active else None, "kernels_available": len(library),
            "max_snow_depth_cm": round(float(snow[~mask].max(initial=0)), 3)}
